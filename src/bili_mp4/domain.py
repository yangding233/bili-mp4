"""Serializable task records and errors shared by the desktop application."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any
import math
import re


class AppError(Exception):
    """A recoverable failure that can be shown in the interface."""


class InputError(AppError):
    """The supplied link or task data is invalid."""


class ResolveError(AppError):
    """Video metadata could not be resolved safely."""


class FormatUnavailableError(ResolveError):
    """The requested format is not available without transcoding."""


class DependencyError(AppError):
    """A required runtime component is missing."""


class StoreError(AppError):
    """Task state could not be read or written."""


TASK_STATUSES = frozenset({
    "queued", "resolving", "downloading", "paused", "checking",
    "merging", "verifying", "committing", "completed", "failed", "cancelled",
})
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_URL_RE = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_message(value: str) -> str:
    """Do not persist expiring media addresses or authorization query strings."""
    return _URL_RE.sub("[链接已隐藏]", str(value))


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InputError(f"{name}必须是正整数")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InputError(f"{name}必须是正整数") from exc
    if result < 1 or str(result) != str(value):
        raise InputError(f"{name}必须是正整数")
    return result


def _optional_number(value: Any, name: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InputError(f"{name}无效")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InputError(f"{name}无效") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise InputError(f"{name}无效")
    return result


@dataclass
class Part:
    bvid: str
    cid: str
    index: int
    title: str
    duration: float | None
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.bvid, str) or not _BVID_RE.fullmatch(self.bvid):
            raise InputError("BV 号无效")
        self.cid = str(_positive_int(self.cid, "CID"))
        self.index = _positive_int(self.index, "分 P 编号")
        self.title = str(self.title).strip() or f"P{self.index}"
        self.duration = _optional_number(self.duration, "时长", positive=True)
        # This field is always a public page URL, never a signed media URL.
        self.url = f"https://www.bilibili.com/video/{self.bvid}?p={self.index}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Part:
        try:
            return cls(**{f.name: data[f.name] for f in fields(cls)})
        except (KeyError, TypeError) as exc:
            raise InputError("分 P 元信息不完整") from exc


@dataclass
class FormatChoice:
    video_id: str
    audio_id: str | None
    height: int
    fps: float | None
    vcodec: str
    acodec: str
    estimated_bytes: int | None

    def __post_init__(self) -> None:
        self.video_id = str(self.video_id).strip()
        if not self.video_id or self.video_id.lower() == "none":
            raise InputError("视频格式标识无效")
        if self.audio_id is not None:
            self.audio_id = str(self.audio_id).strip()
            if not self.audio_id or self.audio_id.lower() == "none":
                raise InputError("音频格式标识无效")
        self.height = _positive_int(self.height, "视频高度")
        self.fps = _optional_number(self.fps, "帧率", positive=True)
        self.vcodec = str(self.vcodec)
        self.acodec = str(self.acodec)
        if self.estimated_bytes is not None:
            self.estimated_bytes = _positive_int(self.estimated_bytes, "预计字节数")

    @property
    def format_selector(self) -> str:
        return f"{self.video_id}+{self.audio_id}" if self.audio_id else self.video_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FormatChoice:
        try:
            return cls(**{f.name: data[f.name] for f in fields(cls)})
        except (KeyError, TypeError) as exc:
            raise InputError("格式元信息不完整") from exc


@dataclass
class Task:
    id: str
    part: Part
    choice: FormatChoice
    output_dir: str
    status: str = "queued"
    message: str = ""
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float = 0
    eta: float | None = None
    output_file: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        try:
            values = {
                f.name: data[f.name]
                for f in fields(cls)
                if f.name in data and f.name not in {"part", "choice"}
            }
            values["part"] = Part.from_dict(data["part"])
            values["choice"] = FormatChoice.from_dict(data["choice"])
            task = cls(**values)
            if not isinstance(task.id, str) or not task.id:
                raise InputError("任务标识无效")
            if task.status not in TASK_STATUSES:
                raise InputError("任务状态无效")
            return task
        except (KeyError, TypeError) as exc:
            raise InputError("任务元信息不完整") from exc
