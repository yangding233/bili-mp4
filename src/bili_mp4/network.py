"""HTTP downloader with bounded retries and conservative validator-based resume."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from .domain import AppError


class Interrupted(AppError):
    """The user paused/cancelled the operation."""


class DownloadError(AppError):
    pass


class RefreshRequired(DownloadError):
    pass


class RetryableError(DownloadError):
    def __init__(self, message: str, wait_seconds: float | None = None):
        super().__init__(message)
        self.wait_seconds = wait_seconds


@dataclass
class RetryBudget:
    limit: int = 5
    used: int = 0

    def consume(self) -> int:
        self.used += 1
        if self.used > self.limit:
            raise DownloadError("网络重试次数已耗尽，已保留可恢复的文件。")
        return self.used


@dataclass
class Resource:
    key: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Probe:
    total: int | None
    etag: str | None
    ranges: bool


_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def strong_etag(value: str | None) -> str | None:
    if value and value.startswith('"') and value.endswith('"'):
        return value
    return None


def content_range(value: str | None) -> tuple[int, int, int]:
    match = _RANGE.fullmatch(value or "")
    if not match:
        raise DownloadError("服务器返回的 Content-Range 无效，停止写入。")
    start, end, total = map(int, match.groups())
    if start > end or end >= total:
        raise DownloadError("服务器返回了不一致的字节范围，停止写入。")
    return start, end, total


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise DownloadError("服务器返回了无效的文件长度。") from exc
    if result < 0:
        raise DownloadError("服务器返回了负数文件长度。")
    return result


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None


def digest_file(path: Path, stop: threading.Event | None = None) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if stop is not None and stop.is_set():
                raise Interrupted("操作已停止。")
            sha.update(chunk)
    return sha.hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class HttpDownloader:
    def __init__(self, opener=None, timeout: float = 20, chunk_size: int = 4 * 1024 * 1024):
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.chunk_size = chunk_size

    def _open(self, resource: Resource, extra: dict[str, str]):
        if not resource.url.startswith(("https://", "http://")):
            raise DownloadError("不支持的媒体地址协议。")
        headers = {**resource.headers, "Accept-Encoding": "identity", **extra}
        request = urllib.request.Request(resource.url, headers=headers)
        try:
            return self.opener(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            code = exc.code
            wait = _retry_after(exc.headers.get("Retry-After")) if exc.headers else None
            exc.close()
            if code in (403, 404, 410):
                raise RefreshRequired("媒体地址失效或当前不可访问，需要重新解析。") from exc
            if code == 416:
                raise RetryableError("服务器拒绝当前范围，将重新确认文件长度。") from exc
            if code == 429 or 500 <= code < 600:
                raise RetryableError(f"服务器暂时不可用（HTTP {code}）。", wait) from exc
            raise DownloadError(f"媒体请求被拒绝（HTTP {code}）。") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
            raise RetryableError("网络连接失败或超时。") from exc

    @staticmethod
    def _check_encoding(response) -> None:
        if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            raise DownloadError("服务器对范围响应使用了内容压缩，无法安全续传。")

    def _probe(self, resource: Resource) -> Probe:
        with self._open(resource, {"Range": "bytes=0-0"}) as response:
            self._check_encoding(response)
            etag = strong_etag(response.headers.get("ETag"))
            if response.status == 206:
                start, end, total = content_range(response.headers.get("Content-Range"))
                if start != 0 or end != 0 or _integer(response.headers.get("Content-Length")) not in (None, 1):
                    raise DownloadError("探测响应范围与请求不一致。")
                if len(response.read(2)) != 1:
                    raise RetryableError("探测响应提前结束或超过预期长度。")
                return Probe(total, etag, True)
            if response.status == 200:
                return Probe(_integer(response.headers.get("Content-Length")), etag, False)
            raise DownloadError(f"不支持的媒体响应（HTTP {response.status}）。")

    @staticmethod
    def _load_meta(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def download(
        self,
        resource: Resource,
        destination: Path,
        stop: threading.Event,
        progress: Callable[[int, int | None], None] = lambda _n, _t: None,
        notice: Callable[[str], None] = lambda _m: None,
        budget: RetryBudget | None = None,
    ) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        manifest = destination.with_name(destination.name + ".json")
        budget = budget or RetryBudget()
        while True:
            if stop.is_set():
                raise Interrupted("操作已停止。")
            try:
                return self._attempt(resource, destination, partial, manifest, stop, progress, notice)
            except RetryableError as exc:
                number = budget.consume()
                delay = exc.wait_seconds if exc.wait_seconds is not None else min(30.0, 2 ** (number - 1)) + random.random()
                notice(f"{exc} {delay:.0f} 秒后重试（{number}/{budget.limit}）。")
                if stop.wait(delay):
                    raise Interrupted("操作已停止。") from exc
            except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, urllib.error.URLError) as exc:
                number = budget.consume()
                delay = min(30.0, 2 ** (number - 1)) + random.random()
                notice(f"连接中断，{delay:.0f} 秒后重试（{number}/{budget.limit}）。")
                if stop.wait(delay):
                    raise Interrupted("操作已停止。") from exc
            except OSError as exc:
                # Disk errors are permanent until the user changes the environment.
                raise DownloadError("无法写入下载文件，请检查磁盘空间、目录权限和文件占用。") from exc

    def _attempt(self, resource, destination, partial, manifest, stop, progress, notice):
        meta = self._load_meta(manifest)
        if meta.get("key") == resource.key and meta.get("complete") and meta.get("sha256"):
            for candidate in (destination, partial):
                if candidate.exists() and candidate.stat().st_size == meta.get("size"):
                    if digest_file(candidate, stop) == meta["sha256"]:
                        if candidate == partial:
                            os.replace(partial, destination)
                        progress(destination.stat().st_size, destination.stat().st_size)
                        return destination
        # Never treat an unverified final stream file as a successful download.
        if destination.exists():
            destination.unlink()
        probe = self._probe(resource)
        if probe.total == 0:
            raise DownloadError("服务器返回了空媒体文件。")
        offset = partial.stat().st_size if partial.exists() else 0
        can_resume = (
            offset > 0 and probe.ranges and probe.etag is not None
            and meta.get("key") == resource.key
            and meta.get("etag") == probe.etag
            and meta.get("total") == probe.total
            and probe.total is not None and offset <= probe.total
        )
        if offset and not can_resume:
            notice("资源缺少一致的强校验信息，重新下载该流以避免错误拼接。")
            partial.unlink()
            offset = 0
        meta = {"key": resource.key, "etag": probe.etag, "total": probe.total, "complete": False}
        atomic_json(manifest, meta)
        progress(offset, probe.total)
        if probe.ranges and probe.etag:
            while probe.total is not None and offset < probe.total:
                if stop.is_set():
                    raise Interrupted("操作已停止。")
                end = min(offset + self.chunk_size, probe.total) - 1
                extra = {"Range": f"bytes={offset}-{end}"}
                if probe.etag:
                    extra["If-Range"] = probe.etag
                with self._open(resource, extra) as response:
                    self._check_encoding(response)
                    if response.status == 200:
                        # If-Range failed or Range was ignored: no bytes may be appended.
                        if partial.exists():
                            partial.unlink()
                        raise RetryableError("服务器忽略范围或资源发生变化，将从头重新确认。")
                    if response.status != 206:
                        raise DownloadError("服务器没有返回预期的范围响应。")
                    start, actual_end, total = content_range(response.headers.get("Content-Range"))
                    length = _integer(response.headers.get("Content-Length"))
                    if (start != offset or actual_end != end or total != probe.total
                            or length not in (None, end - offset + 1)
                            or (probe.etag and strong_etag(response.headers.get("ETag")) != probe.etag)):
                        raise DownloadError("响应范围或资源校验信息不一致，停止追加。")
                    remaining = end - offset + 1
                    with partial.open("ab") as stream:
                        while remaining:
                            if stop.is_set():
                                raise Interrupted("操作已停止。")
                            block = response.read(min(256 * 1024, remaining))
                            if not block:
                                raise RetryableError("范围响应提前结束，已保留下载进度。")
                            if len(block) > remaining:
                                raise DownloadError("范围响应超过请求长度。")
                            stream.write(block)
                            offset += len(block)
                            remaining -= len(block)
                            progress(offset, probe.total)
                        if response.read(1):
                            raise DownloadError("范围响应包含超出请求的数据。")
                        stream.flush()
                        os.fsync(stream.fileno())
        else:
            if stop.is_set():
                raise Interrupted("操作已停止。")
            with self._open(resource, {}) as response:
                self._check_encoding(response)
                if response.status != 200:
                    raise DownloadError("完整下载响应无效。")
                current_total = _integer(response.headers.get("Content-Length"))
                if probe.total is not None and current_total is not None and probe.total != current_total:
                    raise RetryableError("媒体文件长度发生变化，将重新确认。")
                total = current_total if current_total is not None else probe.total
                offset = 0
                with partial.open("wb") as stream:
                    while True:
                        if stop.is_set():
                            raise Interrupted("操作已停止。")
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        if total is not None and offset + len(block) > total:
                            raise DownloadError("下载响应超过声明长度。")
                        stream.write(block)
                        offset += len(block)
                        progress(offset, total)
                    stream.flush()
                    os.fsync(stream.fileno())
                probe.total = total
        size = partial.stat().st_size if partial.exists() else 0
        if not size or (probe.total is not None and size != probe.total):
            raise RetryableError("媒体文件未完整下载，已保留可恢复的内容。")
        meta.update({"complete": True, "size": size, "sha256": digest_file(partial, stop)})
        atomic_json(manifest, meta)
        if stop.is_set():
            raise Interrupted("操作已停止。")
        os.replace(partial, destination)
        progress(size, size)
        return destination
