"""Resolve ordinary, anonymously accessible Bilibili BV videos.

yt-dlp owns format extraction. The public video page supplies CID and expected
full duration; the extra view API can reject requests even when the page works.
Extraction dictionaries (including expiring media URLs) stay in memory.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import threading
import time
from copy import deepcopy
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .domain import (
    AppError, DependencyError, FormatChoice, FormatUnavailableError,
    InputError, Part, ResolveError,
)

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_PATH_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})/?\Z")
_ALLOWED_EXTRACTORS = {"bilibili"}
_TIMEOUT = 20
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_METADATA_TTL = 30
_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_METADATA_LOCK = threading.Lock()


def _http_status(exc: BaseException) -> int | None:
    """Read structured causes first; never interpret numbers in a BV/URL."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, HTTPError):
            return current.code
        if type(current).__name__ == "HTTPError":
            status = getattr(current, "status", None)
            if isinstance(status, int) and 100 <= status <= 599:
                return status
        for field in ("cause", "__cause__", "__context__"):
            cause = getattr(current, field, None)
            if isinstance(cause, BaseException):
                pending.append(cause)
        info = getattr(current, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1 and isinstance(info[1], BaseException):
            pending.append(info[1])
    text = re.sub(r"https?://\S+", "[URL]", str(exc), flags=re.I)
    match = re.search(r"\bHTTP(?:\s+Error)?\s*[:=]?\s*(\d{3})\b", text, re.I)
    return int(match.group(1)) if match else None


def _request_failure(stage: str, exc: BaseException) -> ResolveError:
    status = _http_status(exc)
    context = f"{stage}，HTTP {status}" if status else stage
    if status == 412:
        return ResolveError(
            f"B 站拒绝当前请求（{context}）。请求校验未通过；持续出现时需要检查解析组件，反复重试不保证恢复。"
        )
    if status == 429:
        return ResolveError(f"B 站请求过于频繁（{context}），请暂停操作，等待后再重试。")
    if status in (401, 403):
        return ResolveError(f"当前匿名访问权限不足（{context}），不能下载此内容。")
    if status == 404:
        return ResolveError(f"视频已删除或不可访问（{context}）。")
    text = re.sub(r"https?://\S+", "[URL]", str(exc), flags=re.I).lower()
    if "rate limit" in text or re.search(r"(?<!\d)-352(?!\d)", text):
        return ResolveError(f"B 站拦截了当前请求（{context}，解析器报告请求限制），不能确定等待多久能恢复。")
    if any(word in text for word in ("login", "premium", "supporter", "permission")):
        return ResolveError(f"当前匿名访问权限不足（{context}），不能下载此内容。")
    if any(word in text for word in ("deleted", "geo-restricted")):
        return ResolveError(f"视频已删除、受区域限制或不可访问（{context}）。")
    return ResolveError(f"{context}失败，请检查网络或解析组件版本；原始网络地址未写入日志。")


def normalize_url(value: str) -> str:
    """Accept a BV identifier or an ordinary www.bilibili.com video page."""
    if not isinstance(value, str):
        raise InputError("请输入 BV 号或 B 站视频链接")
    value = value.strip()
    if _BVID_RE.fullmatch(value):
        return f"https://www.bilibili.com/video/{value}"
    if value.startswith("www.bilibili.com/"):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "www.bilibili.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise InputError("首版只支持 www.bilibili.com/video/ 下的 BV 视频链接")
        match = _PATH_RE.fullmatch(parsed.path)
        if not match:
            raise InputError("链接路径无效，请使用普通 BV 视频页面")
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
        part_values = query.get("p")
        suffix = ""
        if part_values is not None:
            if len(part_values) != 1 or not re.fullmatch(r"[0-9]+", part_values[0]):
                raise InputError("p 参数必须是单个正整数")
            index = int(part_values[0])
            if index < 1:
                raise InputError("p 参数必须是正整数")
            suffix = f"?p={index}"
        return f"https://www.bilibili.com/video/{match.group(1)}{suffix}"
    except ValueError as exc:
        raise InputError("视频链接格式无效") from exc


def _bvid(url: str) -> str:
    return _PATH_RE.fullmatch(urlsplit(url).path).group(1)


def _part_index(url: str) -> int:
    return int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])


class _ResolverLogger:
    def __init__(self) -> None:
        self.preview_seen = False

    def debug(self, _: str) -> None:
        pass

    def info(self, _: str) -> None:
        pass

    def warning(self, message: str) -> None:
        text = str(message).lower()
        if "preview" in text or "试看" in text:
            self.preview_seen = True

    def error(self, _: str) -> None:
        pass


def _extract(url: str, *, flat: bool) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise DependencyError("缺少 yt-dlp，请安装项目依赖或使用完整发布包") from exc
    logger = _ResolverLogger()
    options = {
        "quiet": True,
        "logger": logger,
        "skip_download": True,
        "extract_flat": "in_playlist" if flat else False,
        "noplaylist": not flat,
        "ignoreerrors": False,
        "socket_timeout": _TIMEOUT,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
        "usenetrc": False,
        "cookiefile": None,
        "allowed_extractors": ["BiliBili"],
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except AppError:
        raise
    except Exception as exc:
        # Extractor messages may contain signed media URLs. Keep them out of
        # task records and show an actionable category instead.
        raise _request_failure("播放格式解析", exc) from exc
    if not isinstance(info, dict):
        raise ResolveError("解析器未返回可用的视频信息")
    if logger.preview_seen:
        raise ResolveError("当前仅可访问试看内容，首版不下载试看片段")
    return info


def _check_extractor(info: dict[str, Any]) -> None:
    identity = info.get("extractor_key") or info.get("ie_key") or info.get("extractor")
    if not isinstance(identity, str) or identity.lower() not in _ALLOWED_EXTRACTORS:
        raise ResolveError("该链接重定向到了首版不支持的内容类型")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ResolveError("首版仅支持普通点播视频")
    if info.get("has_drm") or info.get("is_drm"):
        raise ResolveError("不支持受 DRM 保护的内容")
    if info.get("is_preview") or info.get("is_paid") or info.get("availability") in {
        "premium_only", "subscriber_only", "needs_auth",
    }:
        raise ResolveError("当前权限不足或仅提供试看内容")


def _metadata_from_page(page: str, bvid: str) -> dict[str, Any]:
    marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", page)
    if marker is None:
        if re.search(r"\bwindow\._riskdata_\s*=", page):
            raise ResolveError("B 站返回了请求验证页面（视频元信息解析），当前匿名请求未通过校验。")
        raise ResolveError("视频页面结构不兼容（视频元信息解析），请更新解析组件。")
    try:
        state, _ = json.JSONDecoder().raw_decode(page[marker.end():])
    except ValueError as exc:
        raise ResolveError("视频页面元信息无效（视频元信息解析），请更新解析组件。") from exc
    if not isinstance(state, dict):
        raise ResolveError("视频页面元信息结构无效。")
    error = state.get("error")
    code = error.get("trueCode") if isinstance(error, dict) else None
    if code in (-403, -101):
        raise ResolveError(f"当前匿名访问权限不足（视频元信息解析，站点代码 {code}）。")
    if code in (-404, 62002, 62004):
        raise ResolveError(f"视频已删除或不可访问（视频元信息解析，站点代码 {code}）。")
    if code in (-352, -412):
        raise ResolveError(f"B 站拒绝当前请求（视频元信息解析，站点代码 {code}），反复重试不保证恢复。")
    if code not in (None, 0):
        raise ResolveError("B 站页面报告了不可访问状态（视频元信息解析）。")
    data = state.get("videoData")
    if not isinstance(data, dict):
        raise ResolveError("该页面不是首版支持的普通 BV 视频。")
    # Keep only public identity and access fields; never cache playback URLs.
    fields = (
        "bvid", "title", "pages", "rights", "redirect_url", "is_upower_exclusive",
        "is_live", "is_preview", "has_drm", "is_paid",
    )
    data = {key: data[key] for key in fields if key in data}
    if data.get("bvid") != bvid:
        raise ResolveError("视频身份校验失败")
    if data.get("redirect_url"):
        raise ResolveError("首版不支持重定向到番剧或付费课程的视频")
    if data.get("is_upower_exclusive") or data.get("is_paid"):
        raise ResolveError("首版不支持充电专属或试看视频")
    rights = data.get("rights") or {}
    if isinstance(rights, dict) and rights.get("is_stein_gate"):
        raise ResolveError("首版不支持互动视频分支")
    if data.get("is_live") or data.get("is_preview") or data.get("has_drm"):
        raise ResolveError("首版仅支持完整、无 DRM 的普通点播视频")
    _metadata_parts(data, bvid)
    return data


def _fetch_metadata(bvid: str) -> dict[str, Any]:
    """Read the ordinary video page, avoiding the redundant view API request."""
    if not isinstance(bvid, str) or not _BVID_RE.fullmatch(bvid):
        raise InputError("BV 号无效")
    now = time.monotonic()
    with _METADATA_LOCK:
        cached = _METADATA_CACHE.get(bvid)
        if cached is not None and now - cached[0] < _METADATA_TTL:
            return deepcopy(cached[1])
    try:
        from yt_dlp.utils.networking import std_headers
    except ImportError as exc:
        raise DependencyError("缺少 yt-dlp，请安装项目依赖或使用完整发布包") from exc
    endpoint = f"https://www.bilibili.com/video/{bvid}/"
    request = Request(endpoint, headers={
        **std_headers, "Accept": "text/html", "Accept-Encoding": "gzip",
        "Referer": "https://www.bilibili.com/",
    })
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            final = urlsplit(response.geturl())
            path = _PATH_RE.fullmatch(final.path)
            if final.scheme != "https" or final.hostname != "www.bilibili.com" or path is None or path.group(1) != bvid:
                raise ResolveError("视频页面重定向到了不支持的内容，停止解析。")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=response) as decoded:
                    body = decoded.read(_MAX_METADATA_BYTES + 1)
            elif encoding in ("", "identity"):
                body = response.read(_MAX_METADATA_BYTES + 1)
            else:
                raise ResolveError("视频页面使用了不支持的压缩格式，停止解析。")
        if len(body) > _MAX_METADATA_BYTES:
            raise ResolveError("视频页面元信息异常过大")
        data = _metadata_from_page(body.decode("utf-8-sig"), bvid)
    except HTTPError as exc:
        raise _request_failure("获取视频页面", exc) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise _request_failure("获取视频页面元信息", exc) from exc
    with _METADATA_LOCK:
        if len(_METADATA_CACHE) >= 32:
            oldest = min(_METADATA_CACHE, key=lambda key: _METADATA_CACHE[key][0])
            del _METADATA_CACHE[oldest]
        _METADATA_CACHE[bvid] = (time.monotonic(), deepcopy(data))
    return data


def _metadata_parts(data: dict[str, Any], bvid: str) -> list[Part]:
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ResolveError("未找到有效的分 P 列表")
    parts: list[Part] = []
    seen_cids: set[str] = set()
    for position, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get("page") != position:
            raise ResolveError("分 P 列表编号异常")
        try:
            part = Part(
                bvid=bvid, cid=str(page["cid"]), index=position,
                title=page.get("part") or data.get("title") or f"P{position}",
                duration=page.get("duration"),
                url=f"https://www.bilibili.com/video/{bvid}?p={position}",
            )
        except (KeyError, InputError) as exc:
            raise ResolveError("分 P 的 CID 或时长无效") from exc
        if part.duration is None:
            raise ResolveError("缺少完整时长，无法安全排除试看内容")
        if part.cid in seen_cids:
            raise ResolveError("分 P 列表包含重复 CID")
        seen_cids.add(part.cid)
        parts.append(part)
    return parts


def resolve_parts(url: str) -> list[Part]:
    """List only the parts belonging to this BV, regardless of incoming p."""
    normalized = normalize_url(url)
    bvid = _bvid(normalized)
    parts = _metadata_parts(_fetch_metadata(bvid), bvid)
    if _part_index(normalized) > len(parts):
        raise InputError("链接指定的分 P 不存在")
    return parts


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _codec_rank(vcodec: str) -> int:
    codec = vcodec.lower()
    if codec.startswith(("avc1", "avc3", "h264")):
        return 3
    if codec.startswith(("hvc1", "hev1", "hevc", "h265")):
        return 2
    if codec.startswith(("av01", "av1")):
        return 1
    return 0


def _is_aac(acodec: str) -> bool:
    return acodec.lower().startswith(("mp4a", "aac"))


def _direct_http(fmt: dict[str, Any]) -> bool:
    if fmt.get("has_drm") or fmt.get("is_drm") or fmt.get("fragments"):
        return False
    address = fmt.get("url")
    if not isinstance(address, str):
        return False
    parsed = urlsplit(address)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and not parsed.username
        and not parsed.password
        and fmt.get("protocol") in (None, "http", "https")
    )


def _estimate(fmt: dict[str, Any], duration: float | None) -> int | None:
    for field in ("filesize", "filesize_approx"):
        size = _number(fmt.get(field))
        if size is not None:
            return int(size)
    bitrate = _number(fmt.get("tbr"))
    if bitrate is not None and duration:
        return int(bitrate * 1000 * duration / 8)
    return None


def choices_from_info(info: dict[str, Any]) -> list[FormatChoice]:
    """Build copy-compatible choices; no playback request or transcoding."""
    formats = info.get("formats") or []
    if not isinstance(formats, list):
        raise ResolveError("播放格式列表无效")
    duration = _number(info.get("duration"))
    audio_formats = [
        f for f in formats if isinstance(f, dict)
        and f.get("format_id") is not None
        and f.get("ext") in {"m4a", "mp4"}
        and f.get("vcodec") == "none"
        and _is_aac(str(f.get("acodec", "")))
        and _direct_http(f)
    ]
    best_audio = max(audio_formats, key=lambda f: (
        _number(f.get("abr")) or _number(f.get("tbr")) or 0,
        _number(f.get("asr")) or 0,
        _number(f.get("audio_channels")) or 0,
    ), default=None)
    choices: list[FormatChoice] = []
    seen: set[tuple[str, str | None]] = set()
    for video in formats:
        if not isinstance(video, dict) or not _direct_http(video):
            continue
        height = _number(video.get("height"))
        if (
            video.get("format_id") is None or video.get("ext") != "mp4"
            or not _codec_rank(str(video.get("vcodec", "")))
            or height is None or not height.is_integer()
        ):
            continue
        vcodec = str(video["vcodec"])
        acodec = str(video.get("acodec", ""))
        audio_id: str | None
        video_size = _estimate(video, duration)
        if acodec == "none":
            if best_audio is None:
                continue
            audio_id = str(best_audio["format_id"])
            acodec = str(best_audio["acodec"])
            audio_size = _estimate(best_audio, duration)
            size = video_size + audio_size if video_size and audio_size else None
        elif _is_aac(acodec):
            audio_id = None
            size = video_size
        else:
            continue
        identity = (str(video["format_id"]), audio_id)
        if identity in seen:
            continue
        seen.add(identity)
        choices.append(FormatChoice(
            video_id=identity[0], audio_id=audio_id, height=int(height),
            fps=_number(video.get("fps")), vcodec=vcodec, acodec=acodec,
            estimated_bytes=size,
        ))
    choices.sort(key=_choice_key, reverse=True)
    return choices


def _choice_key(choice: FormatChoice) -> tuple[int, int, float, int]:
    return (
        _codec_rank(choice.vcodec), choice.height, choice.fps or 0,
        choice.estimated_bytes or 0,
    )


def resolve_formats(part: Part) -> tuple[dict[str, Any], list[FormatChoice]]:
    """Resolve one selected P and verify its identity and full duration."""
    pages = _metadata_parts(_fetch_metadata(part.bvid), part.bvid)
    if part.index > len(pages) or pages[part.index - 1].cid != str(part.cid):
        raise ResolveError("分 P 身份已经变化，请重新解析并选择")
    expected = pages[part.index - 1]
    info = _extract(part.url, flat=False)
    _check_extractor(info)
    if info.get("_type") in {"playlist", "multi_video", "url", "url_transparent"}:
        raise ResolveError("该分 P 返回了不支持的播放类型")
    accepted_ids = {f"{part.bvid}_p{part.index}"}
    if part.index == 1:
        accepted_ids.add(part.bvid)
    if str(info.get("id", "")) not in accepted_ids:
        raise ResolveError("解析得到的分 P 身份不一致")
    webpage = info.get("webpage_url")
    if webpage:
        try:
            canonical = normalize_url(webpage)
        except InputError as exc:
            raise ResolveError("播放信息来自不支持的页面") from exc
        if _bvid(canonical) != part.bvid or _part_index(canonical) != part.index:
            raise ResolveError("播放信息与选中的分 P 不一致")
    duration = _number(info.get("duration"))
    if duration is None or expected.duration is None:
        raise ResolveError("缺少完整时长，无法安全排除试看内容")
    tolerance = max(3.0, expected.duration * 0.03)
    if abs(duration - expected.duration) > tolerance:
        raise ResolveError("播放时长与完整视频不一致，可能只提供试看内容")
    choices = choices_from_info(info)
    if not choices:
        raise FormatUnavailableError(
            "未找到可直接封装为 MP4 的视频和 AAC 音频；首版不进行转码"
        )
    return info, choices


def select_choice(choices: list[FormatChoice], height: int | None = None) -> FormatChoice:
    """Prefer compatibility; an explicit height is an exact requirement."""
    if height is not None and (
        isinstance(height, bool) or not isinstance(height, int) or height < 1
    ):
        raise InputError("清晰度必须是正整数高度")
    candidates = [c for c in choices if height is None or c.height == height]
    if not candidates:
        if height is not None:
            raise FormatUnavailableError(f"当前分 P 没有可用的 {height}p 格式")
        raise FormatUnavailableError("没有可用的 MP4 格式")
    return max(candidates, key=_choice_key)
