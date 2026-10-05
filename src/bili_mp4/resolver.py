"""Resolve ordinary, anonymously accessible Bilibili BV videos.

yt-dlp owns extraction. Public view metadata supplies CID and expected full
duration, which yt-dlp's flat anthology entries currently do not expose.
Extraction dictionaries (including expiring media URLs) stay in memory.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
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
        text = str(exc).lower()
        if any(word in text for word in ("login", "premium", "supporter", "403", "permission")):
            raise ResolveError("当前匿名访问权限不足，不能下载此内容") from exc
        if any(word in text for word in ("429", "rate limit", "412", "-352")):
            raise ResolveError("B 站暂时限制请求，请稍后重试") from exc
        if any(word in text for word in ("404", "deleted", "geo-restricted")):
            raise ResolveError("视频已删除、受区域限制或不可访问") from exc
        raise ResolveError("播放信息解析失败，请检查网络或更新 yt-dlp 后重试") from exc
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


def _fetch_metadata(bvid: str) -> dict[str, Any]:
    """Read ordinary public metadata; never request playback permissions."""
    endpoint = "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid})
    request = Request(endpoint, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Accept": "application/json",
    })
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read(_MAX_METADATA_BYTES + 1)
        if len(body) > _MAX_METADATA_BYTES:
            raise ResolveError("视频元信息异常过大")
        result = json.loads(body)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ResolveError("当前匿名权限无法读取此视频") from exc
        if exc.code in (412, 429):
            raise ResolveError("B 站暂时限制请求，请稍后重试") from exc
        raise ResolveError("无法读取视频元信息，请稍后重试") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ResolveError("读取视频元信息失败，请检查网络后重试") from exc
    if not isinstance(result, dict) or result.get("code") != 0:
        code = result.get("code") if isinstance(result, dict) else None
        if code in (-403, -101):
            raise ResolveError("当前匿名访问权限不足")
        if code in (-404, 62002, 62004):
            raise ResolveError("视频已删除或不可访问")
        if code in (-352, -412):
            raise ResolveError("B 站暂时限制请求，请稍后重试")
        raise ResolveError("B 站未返回可用的视频元信息")
    data = result.get("data")
    if not isinstance(data, dict) or data.get("bvid") != bvid:
        raise ResolveError("视频身份校验失败")
    if data.get("redirect_url"):
        raise ResolveError("首版不支持重定向到番剧或付费课程的视频")
    if data.get("is_upower_exclusive"):
        raise ResolveError("首版不支持充电专属或试看视频")
    rights = data.get("rights") or {}
    if isinstance(rights, dict) and rights.get("is_stein_gate"):
        raise ResolveError("首版不支持互动视频分支")
    if data.get("is_live") or data.get("is_preview") or data.get("has_drm"):
        raise ResolveError("首版仅支持完整、无 DRM 的普通点播视频")
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
    base = f"https://www.bilibili.com/video/{bvid}"
    info = _extract(base, flat=True)
    _check_extractor(info)
    if info.get("_type") == "multi_video":
        raise ResolveError("首版不支持旧版分段 FLV 视频")
    extracted_indices: set[int] | None = None
    if info.get("_type") == "playlist":
        extracted_indices = set()
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                raise ResolveError("解析器返回了无效选集")
            _check_extractor(entry)
            try:
                page_url = normalize_url(entry.get("url") or entry.get("webpage_url") or "")
            except InputError as exc:
                raise ResolveError("选集包含不支持的页面") from exc
            if _bvid(page_url) != bvid:
                raise ResolveError("拒绝扩大下载范围到其他 BV")
            index = _part_index(page_url)
            if index in extracted_indices:
                raise ResolveError("解析器返回了重复选集")
            extracted_indices.add(index)
        if not extracted_indices:
            raise ResolveError("未找到可访问的选集")
    else:
        extracted_id = str(info.get("id", ""))
        if extracted_id not in {bvid, f"{bvid}_p1"}:
            raise ResolveError("解析得到的视频身份不一致")
    parts = _metadata_parts(_fetch_metadata(bvid), bvid)
    if extracted_indices is not None and extracted_indices != {p.index for p in parts}:
        raise ResolveError("解析器与公开元信息的选集列表不一致，请稍后重试")
    if extracted_indices is None and len(parts) != 1:
        raise ResolveError("解析器未返回完整选集列表，请更新 yt-dlp 后重试")
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
