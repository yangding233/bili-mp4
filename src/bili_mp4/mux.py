"""Local media probing and lossless MP4 muxing (no shell execution)."""
from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .domain import AppError, DependencyError
from .network import Interrupted


class MediaError(AppError):
    pass


def find_tools(tool_dir: str | None = None) -> tuple[str, str]:
    explicit = tool_dir or os.environ.get("BILI_MP4_TOOLS") or os.environ.get("BILI_MP4_FFMPEG_DIR")
    suffix = ".exe" if os.name == "nt" else ""
    if explicit:
        folder = Path(explicit)
        pair = (folder / ("ffmpeg" + suffix), folder / ("ffprobe" + suffix))
        if all(p.is_file() for p in pair):
            return tuple(str(p.resolve()) for p in pair)
        raise DependencyError("所选工具目录需要同时包含 ffmpeg 和 ffprobe。")
    roots = [
        Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools",
        Path(sys.executable).parent / "tools",
        Path(__file__).resolve().parents[2] / "tools",
    ]
    for root in roots:
        pair = (root / ("ffmpeg" + suffix), root / ("ffprobe" + suffix))
        if all(p.is_file() for p in pair):
            return tuple(str(p.resolve()) for p in pair)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    raise DependencyError("未找到 FFmpeg/ffprobe，请选择工具目录，或使用包含工具的 Windows 便携包。")


def _process(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def inspect_media(path: Path, ffprobe: str, stop: threading.Event | None = None) -> dict:
    args = [ffprobe, "-v", "error", "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,duration,width,height",
            "-of", "json", str(path)]
    process = _process(args)
    deadline = time.monotonic() + 30
    while True:
        if stop is not None and stop.is_set():
            _terminate(process)
            raise Interrupted("媒体检查已停止。")
        if time.monotonic() > deadline:
            _terminate(process)
            raise MediaError("媒体检查超时。")
        try:
            stdout, _stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    if process.returncode:
        raise MediaError("媒体文件无法读取，已保留下载文件供重试。")
    try:
        info = json.loads(stdout)
    except ValueError as exc:
        raise MediaError("媒体检查结果无效。") from exc
    return info


def _duration(value) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def validate_media(
    info: dict, *, video: bool, audio: bool, expected_duration: float | None = None,
    height: int | None = None,
) -> None:
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if video and not videos:
        raise MediaError("媒体文件缺少视频流。")
    if audio and not audios:
        raise MediaError("媒体文件缺少音频流。")
    if height and videos and videos[0].get("height") != height:
        raise MediaError("视频分辨率与所选格式不一致。")
    format_duration = _duration(info.get("format", {}).get("duration"))
    required = (videos[:1] if video else []) + (audios[:1] if audio else [])
    durations = [_duration(s.get("duration")) or format_duration for s in required]
    if not durations or any(d is None for d in durations):
        raise MediaError("无法确认媒体时长。")
    tolerance = max(3.0, (expected_duration or max(durations)) * 0.005)
    if expected_duration and any(abs(d - expected_duration) > tolerance for d in durations):
        raise MediaError("媒体时长与分 P 时长不一致，可能只取得预览或残缺内容。")
    if len(durations) > 1 and max(durations) - min(durations) > tolerance:
        raise MediaError("音视频时长差异过大，需要检查输入文件。")


def mux_mp4(
    ffmpeg: str, video_path: Path, audio_path: Path | None, target: Path,
    duration: float | None, stop: threading.Event,
    progress: Callable[[float], None] = lambda _p: None,
) -> None:
    if target.exists():
        target.unlink()  # This path is task-owned temporary output, never a final MP4.
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(video_path)]
    if audio_path is not None:
        args += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0"]
    args += ["-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(target)]
    process = _process(args)
    events: queue.Queue[str] = queue.Queue()
    errors: deque[str] = deque(maxlen=40)

    def read_output():
        for line in process.stdout:
            events.put(line.strip())

    def read_errors():
        for line in process.stderr:
            errors.append(line.strip())

    readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None or not events.empty():
            if stop.is_set():
                raise Interrupted("合并已停止，可重新执行合并。")
            try:
                line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if line.startswith("out_time_us=") and duration:
                try:
                    progress(min(0.99, max(0.0, int(line.split("=", 1)[1]) / 1_000_000 / duration)))
                except ValueError:
                    pass
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        if process.returncode:
            # Do not expose raw process errors containing private local paths.
            raise MediaError("FFmpeg 无损封装失败。输入流已保留，可重试合并或重新选择格式。")
        progress(1.0)
    finally:
        _terminate(process)
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
