"""Bounded anonymous Bilibili acceptance; never invoked by ordinary pushes.

Media stays in an OS temporary directory. Only sanitized result JSON is kept.
No cookies, credentials, signed playback URLs, or downloaded media are printed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from bili_mp4 import resolver
from bili_mp4.domain import (
    AppError, DependencyError, FormatUnavailableError, redact_message,
)
from bili_mp4.engine import DownloadManager
from bili_mp4.mux import find_tools, inspect_media, validate_media
from bili_mp4.network import HttpDownloader, Interrupted
from bili_mp4.store import TaskStore

EXAMPLE_BVID = "BV12jup6AEVi"
EXAMPLE_URL = f"https://www.bilibili.com/video/{EXAMPLE_BVID}?p=2"
MEDIA_LIMIT = 32 * 1024 * 1024
DEADLINE_SECONDS = 5 * 60


class DownloadLimitError(AppError):
    pass


class Deadline:
    def __init__(self):
        self.end = time.monotonic() + DEADLINE_SECONDS
        self.stop = threading.Event()

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.end

    def check(self) -> None:
        if self.expired:
            self.stop.set()
            raise Interrupted("真实视频验收已超过 5 分钟时限")

    def remaining(self) -> float:
        self.check()
        return max(0.1, self.end - time.monotonic())


class MeteredResponse:
    """Count actual response-body reads, including retransmission and probes."""
    def __init__(self, response, downloader):
        self.response = response
        self.downloader = downloader

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def read(self, amount=-1):
        self.downloader.deadline.check()
        remaining = MEDIA_LIMIT - self.downloader.body_bytes
        if remaining <= 0:
            raise DownloadLimitError("媒体读取量已经达到 32 MiB 下载上限")
        if amount is None or amount < 0:
            amount = remaining
        block = self.response.read(min(amount, remaining))
        self.downloader.body_bytes += len(block)
        self.downloader.deadline.check()
        return block


class BoundedDownloader(HttpDownloader):
    def __init__(self, deadline):
        super().__init__(timeout=15)
        self.deadline = deadline
        self.totals: dict[str, int] = {}
        self.body_bytes = 0

    def _open(self, resource, extra):
        self.deadline.check()
        return MeteredResponse(super()._open(resource, extra), self)

    def _probe(self, resource):
        self.deadline.check()
        probe = super()._probe(resource)
        if probe.total is None:
            raise DownloadLimitError("媒体长度未知，拒绝执行有下载上限的验收")
        if probe.total <= 0:
            raise DownloadLimitError("媒体长度无效，拒绝下载")
        # A retry of the same stream must not count its declared size twice.
        proposed = {**self.totals, resource.key: probe.total}
        if sum(proposed.values()) > MEDIA_LIMIT:
            raise DownloadLimitError("所选两路媒体超过 32 MiB 下载上限")
        self.totals = proposed
        return probe


class StatusReporter:
    def __init__(self):
        self.previous = None

    def __call__(self, event):
        if event.get("type") != "task":
            return
        status = event["task"]["status"]
        if status != self.previous:
            self.previous = status
            print(f"stage={status}", file=sys.stderr, flush=True)


def failure_category(exc: BaseException, deadline: Deadline) -> str:
    if deadline.expired:
        return "deadline"
    if isinstance(exc, DownloadLimitError):
        return "download_limit"
    if isinstance(exc, FormatUnavailableError):
        return "format_unavailable"
    if isinstance(exc, DependencyError):
        return "dependency"
    text = str(exc)
    if "下载上限" in text or "媒体长度" in text:
        return "download_limit"
    if "HTTP 412" in text or "拒绝当前请求" in text or "请求验证" in text:
        return "request_rejected"
    if "限制请求" in text or "限流" in text or "过于频繁" in text:
        return "rate_limit"
    if any(word in text for word in ("权限", "试看", "不可访问", "已删除")):
        return "access"
    if any(word in text for word in ("网络", "连接", "解析", "元信息")):
        return "network_or_extractor"
    if any(word in text for word in ("FFmpeg", "封装", "解码", "音频", "视频流", "时长")):
        return "media"
    if isinstance(exc, (Interrupted, KeyboardInterrupt)):
        return "interrupted"
    return "application"


def run_check(bvid: str = EXAMPLE_BVID, part_index: int = 2, height: int = 720) -> dict:
    normalized = resolver.normalize_url(bvid)
    if normalized != f"https://www.bilibili.com/video/{bvid}" or part_index < 1 or height < 1:
        raise ValueError("A BV identifier, positive part and exact pixel height are required")
    deadline = Deadline()
    directory = Path(tempfile.mkdtemp(prefix="bili-mp4-live-check-"))
    store = None
    manager = None
    timer = None
    result = {"status": "failed", "bvid": bvid, "part": part_index}
    try:
        store = TaskStore(directory / "tasks.sqlite3")
        downloader = BoundedDownloader(deadline)
        manager = DownloadManager(store, StatusReporter(), downloader=downloader)
        manager._stop = deadline.stop
        timer = threading.Timer(deadline.remaining(), deadline.stop.set)
        timer.daemon = True
        timer.start()
        parts = resolver.resolve_parts(f"{normalized}?p={part_index}")
        deadline.check()
        part = next((item for item in parts if item.index == part_index), None)
        if (
            part is None or part.bvid != bvid or int(part.cid) <= 0
        ):
            raise AppError("示例视频未返回有效的所选分 P 元信息")
        _info, choices = resolver.resolve_formats(part)
        choice = resolver.select_choice(choices, height=height)
        deadline.check()
        task = manager.enqueue([(part, choice)], directory / "media")[0]
        manager.start()
        while True:
            deadline.check()
            with manager._lock:
                status, message = task.status, task.message
            if status == "completed":
                break
            if status in {"failed", "paused", "cancelled"}:
                raise AppError(message or "真实视频任务失败")
            time.sleep(0.1)
        ffmpeg, ffprobe = find_tools()
        final = Path(task.output_file)
        probe = inspect_media(final, ffprobe, deadline.stop)
        validate_media(
            probe, video=True, audio=True, expected_duration=part.duration,
            height=height,
        )
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-xerror", "-i", str(final),
             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=min(120, deadline.remaining()),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if decode.returncode:
            raise AppError("完整音视频解码检查失败")
        deadline.check()
        streams = probe["streams"]
        video_codec = next(s["codec_name"] for s in streams if s["codec_type"] == "video")
        audio_codec = next(s["codec_name"] for s in streams if s["codec_type"] == "audio")
        duration = float(probe["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise AppError("成品时长无效")
        result = {
            "status": "success", "bvid": part.bvid, "part": part.index,
            "resolution": {"height": choice.height, "fps": choice.fps},
            "bytes": {
                "downloaded": task.downloaded_bytes,
                "media_read": downloader.body_bytes,
                "mp4": final.stat().st_size,
            },
            "duration": duration,
            "codecs": {"video": video_codec, "audio": audio_codec},
        }
    except (Exception, KeyboardInterrupt) as exc:
        result.update({
            "status": "failed", "category": failure_category(exc, deadline),
            "message": redact_message(str(exc)) if isinstance(exc, AppError)
            else "真实视频验收未完成，请查看脱敏类别",
        })
    finally:
        if timer is not None:
            timer.cancel()
        stopped = manager.shutdown(timeout=30) if manager is not None else True
        if stopped:
            if store is not None:
                store.close()
            try:
                temp_root = Path(tempfile.gettempdir()).resolve()
                resolved_directory = directory.resolve()
                if (
                    resolved_directory.parent != temp_root
                    or not resolved_directory.name.startswith("bili-mp4-live-check-")
                ):
                    raise AppError("临时目录位置异常，拒绝清理")
                shutil.rmtree(resolved_directory)
                result["temporary_files_removed"] = True
            except (OSError, AppError):
                result.update({
                    "status": "failed", "category": "cleanup",
                    "message": "临时目录未能完全清理；媒体未上传",
                    "temporary_files_removed": False,
                })
        else:
            # Do not race deletion against an active writer. The CI runner is
            # disposable and this directory is never included in artifacts.
            result.update({
                "status": "failed", "category": "worker_shutdown",
                "message": "后台未在停止时限内退出；未删除仍在使用的临时目录，媒体未上传",
                "temporary_files_removed": False,
            })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性匿名实网验收，32 MiB/5分钟上限，媒体不上传")
    parser.add_argument("--bvid", default=EXAMPLE_BVID)
    parser.add_argument("--part", type=int, default=2)
    parser.add_argument("--height", type=int, default=720, help="Exact stored pixel height, including portrait videos")
    parser.add_argument("--result-file", type=Path, default=Path("build/live-check.json"))
    args = parser.parse_args()
    result = run_check(args.bvid, args.part, args.height)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(encoded + "\n", encoding="utf-8")
    print(encoded, flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
