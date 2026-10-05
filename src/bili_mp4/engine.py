"""Persistent queue orchestration. UI code never performs downloads directly."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .domain import AppError, Part, FormatChoice, Task
from .store import TaskStore
from . import resolver
from .network import (
    HttpDownloader, Interrupted, RefreshRequired, Resource, RetryBudget,
    atomic_json, digest_file,
)
from .mux import MediaError, find_tools, inspect_media, validate_media, mux_mp4

_ACTIVE = {"resolving", "downloading", "checking", "merging", "verifying", "committing"}
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+")


def redact(message: str) -> str:
    return _URL.sub("[链接已隐藏]", str(message))


def safe_name(value: str, maximum: int = 65) -> str:
    clean = _FORBIDDEN.sub("_", value).strip().rstrip(". ")
    if not clean:
        clean = "未命名"
    if _RESERVED.match(clean):
        clean = "_" + clean
    return clean[:maximum].rstrip(". ") or "未命名"


def _workdir(task: Task) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", task.id):
        raise AppError("任务编号无效。")
    return Path(task.output_dir) / ".bili-mp4" / task.id


def _task_key(task: Task) -> tuple:
    return (
        task.part.bvid, str(task.part.cid), task.choice.video_id, task.choice.audio_id,
        str(Path(task.output_dir).resolve()),
    )


def _commit_no_overwrite(temporary: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, target)  # Windows rename does not replace an existing file.
    else:
        os.link(temporary, target)  # Atomic no-clobber publication for POSIX tests.
        temporary.unlink()


class DownloadManager:
    def __init__(
        self, store: TaskStore, emit_callback: Callable[[dict], None],
        tool_dir: str | None = None, *, downloader=None, resolver_module=None,
    ):
        self.store = store
        self.emit_callback = emit_callback
        self.tool_dir = tool_dir
        self.downloader = downloader or HttpDownloader()
        self.resolver = resolver_module or resolver
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_id: str | None = None
        self._control: str | None = None
        self._storage_blocked = False
        self.tasks = {task.id: task for task in self.store.load_all()}
        for task in self.tasks.values():
            if task.status in _ACTIVE:
                task.status = "paused"
                task.message = "上次运行已中断，点击继续后检查文件并恢复。"
                self.store.save(task)
            elif task.status == "completed" and (not task.output_file or not Path(task.output_file).is_file()):
                task.status = "failed"
                task.message = "成品文件已移动或删除，请检查输出位置。"
                self.store.save(task)

    def _emit(self, event: dict) -> None:
        try:
            self.emit_callback(event)
        except Exception:
            # A closed UI must not corrupt a successfully downloaded file.
            pass

    def _publish(self, task: Task, *, save: bool = True) -> None:
        with self._lock:
            task.message = redact(task.message)
            if save:
                self.store.save(task)
                self._storage_blocked = False
            data = task.to_dict()
        self._emit({"type": "task", "task": data})

    def _set(self, task: Task, status: str, message: str = "") -> None:
        with self._lock:
            task.status, task.message = status, message
        self._publish(task)

    def enqueue(self, parts_and_choices: list[tuple[Part, FormatChoice]], output_dir: Path) -> list[Task]:
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        # Exclusive creation verifies write permission without deleting user files.
        sentinel = output_dir / (".bili-write-test-" + uuid.uuid4().hex)
        with sentinel.open("xb") as stream:
            stream.write(b"ok")
        sentinel.unlink()
        created = []
        with self._lock:
            known = {_task_key(task) for task in self.tasks.values() if task.status != "cancelled"}
            for part, choice in parts_and_choices:
                task = Task(id=uuid.uuid4().hex, part=part, choice=choice, output_dir=str(output_dir))
                key = _task_key(task)
                if key in known:
                    continue
                self.tasks[task.id] = task
                known.add(key)
                created.append(task)
                self._publish(task)
        self._wake.set()
        return created

    def start(self) -> None:
        with self._lock:
            if self._shutdown.is_set():
                raise AppError("下载管理器已关闭。")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="bili-download-queue", daemon=True)
                self._thread.start()
        self._wake.set()

    def pause(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks[task_id]
            if task_id == self._active_id:
                self._control = "paused"
                self._stop.set()
                task.message = "正在停止当前操作并保存文件……"
                self._publish(task)
            elif task.status == "queued":
                self._set(task, "paused", "任务已暂停。")

    def resume(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks[task_id]
            if task.status not in {"paused", "failed", "cancelled"}:
                return
            if task_id == self._active_id:
                return
            self._set(task, "queued", "准备检查已有文件并继续。")
        self.start()

    def retry(self, task_id: str) -> None:
        self.resume(task_id)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks[task_id]
            if task.status == "completed":
                return
            if task_id == self._active_id:
                self._control = "cancelled"
                self._stop.set()
                task.message = "正在取消，已下载内容将保留……"
                self._publish(task)
            else:
                self._set(task, "cancelled", "任务已取消，已下载内容保留。")

    def shutdown(self, timeout: float = 2.0) -> bool:
        self._shutdown.set()
        with self._lock:
            if self._active_id is not None:
                self._control = "paused"
                self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return not (thread and thread.is_alive())

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            with self._lock:
                task = None if self._storage_blocked else next((t for t in self.tasks.values() if t.status == "queued"), None)
                if task:
                    self._active_id = task.id
                    self._control = None
                    self._stop.clear()
                    task.status = "resolving"
            if task is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                self._execute(task)
            except Interrupted:
                self._failure(task, self._control or "paused", "操作已停止，已下载内容保留。")
            except Exception as exc:
                if self._control:
                    self._failure(task, self._control, "操作已停止，已下载内容保留。")
                else:
                    message = str(exc) if isinstance(exc, AppError) else "任务失败，请检查输出目录、磁盘空间或重试。"
                    self._failure(task, "failed", message)
            finally:
                with self._lock:
                    self._active_id = None
                    self._control = None

    def _failure(self, task: Task, status: str, message: str) -> None:
        try:
            self._set(task, status, message)
        except Exception:
            with self._lock:
                self._storage_blocked = True
                task.status = "failed"
                task.message = "无法保存任务数据库，队列已暂停。请修复磁盘空间或目录权限后重试。"
            self._publish(task, save=False)
            self._emit({"type": "log", "message": task.message})

    def _check_stop(self) -> None:
        if self._stop.is_set() or self._shutdown.is_set():
            raise Interrupted("操作已停止。")

    def _output(self, task: Task) -> Path:
        folder = Path(task.output_dir) / safe_name(f"{task.part.bvid}")
        folder.mkdir(parents=True, exist_ok=True)
        if task.output_file:
            existing = Path(task.output_file)
            if existing.parent.resolve() != folder.resolve():
                raise AppError("任务输出路径与选定目录不一致。")
            if not existing.exists():
                return existing
        name = safe_name(task.part.title)
        stem = f"P{task.part.index:03d}_{name}_{task.choice.height}p"
        target = folder / (stem + ".mp4")
        suffix = 2
        occupied = {t.output_file for t in self.tasks.values() if t.id != task.id and t.output_file}
        while target.exists() or str(target) in occupied:
            target = folder / f"{stem}_{suffix}.mp4"
            suffix += 1
        if os.name == "nt" and len(str(target)) > 240:
            raise AppError("输出路径过长，请选择更短的保存目录。")
        task.output_file = str(target)
        self._publish(task)
        return target

    def _recover_commit(self, task: Task, directory: Path, ffprobe: str) -> bool:
        receipt_path = directory / "commit.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if receipt.get("task_id") != task.id or receipt.get("key") != list(_task_key(task)):
            return False
        target = Path(task.output_file) if task.output_file else None
        if not target or str(target) != receipt.get("target") or not target.is_file():
            return False
        if target.stat().st_size != receipt.get("size") or digest_file(target, self._stop) != receipt.get("sha256"):
            raise AppError("已提交文件与任务记录不一致，未覆盖成品，请检查输出文件。")
        validate_media(inspect_media(target, ffprobe, self._stop), video=True, audio=True,
                       expected_duration=task.part.duration, height=task.choice.height)
        self._set(task, "completed", "已恢复上次成功生成的 MP4。")
        self._cleanup_inputs(directory)
        return True

    @staticmethod
    def _cleanup_inputs(directory: Path) -> None:
        # Only remove our explicitly named files; retain the publication receipt.
        for name in ("video.media", "audio.media", "video.media.part", "audio.media.part",
                     "video.media.json", "audio.media.json", "merged.partial.mp4"):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _resolve_selected(self, task: Task) -> dict:
        self._check_stop()
        info, choices = self.resolver.resolve_formats(task.part)
        choice = next((c for c in choices if c.video_id == task.choice.video_id
                       and c.audio_id == task.choice.audio_id and c.height == task.choice.height
                       and c.vcodec == task.choice.vcodec and c.acodec == task.choice.acodec), None)
        if choice is None:
            raise AppError("原选定格式当前不可用；未自动降级，请重新解析并选择格式。")
        return info

    def _resource(self, task: Task, info: dict, format_id: str) -> Resource:
        fmt = next((f for f in info.get("formats", []) if str(f.get("format_id")) == format_id), None)
        if not fmt or not fmt.get("url"):
            raise AppError("选定格式没有可下载的媒体地址。")
        if fmt.get("has_drm") or fmt.get("protocol") not in (None, "https", "http"):
            raise AppError("首版仅支持非 DRM 的直接 HTTP 媒体流。")
        return Resource(
            key=f"{task.part.bvid}:{task.part.cid}:{format_id}",
            url=fmt["url"], headers={**(info.get("http_headers") or {}), **(fmt.get("http_headers") or {})},
        )

    def _have_verified_inputs(self, task: Task, directory: Path, ffprobe: str) -> bool:
        """Reuse complete, task-bound inputs without requiring network access."""
        ids = [task.choice.video_id] + ([task.choice.audio_id] if task.choice.audio_id else [])
        for index, format_id in enumerate(ids):
            self._check_stop()
            path = directory / ("video.media" if index == 0 else "audio.media")
            manifest = path.with_name(path.name + ".json")
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
                if (
                    not isinstance(meta, dict) or meta.get("complete") is not True
                    or meta.get("key") != f"{task.part.bvid}:{task.part.cid}:{format_id}"
                    or not path.is_file() or path.stat().st_size != meta.get("size")
                    or not isinstance(meta.get("sha256"), str)
                ):
                    return False
                verified = digest_file(path, self._stop) == meta["sha256"]
            except (OSError, ValueError, TypeError):
                return False
            if not verified:
                manifest.unlink(missing_ok=True)
                return False
            try:
                validate_media(
                    inspect_media(path, ffprobe, self._stop),
                    video=index == 0, audio=index == 1 or task.choice.audio_id is None,
                    expected_duration=task.part.duration,
                    height=task.choice.height if index == 0 else None,
                )
            except MediaError:
                # Invalidate only this stream. A missing tool or user interruption
                # propagates without invalidating otherwise complete inputs.
                manifest.unlink(missing_ok=True)
                return False
        return True

    def _execute(self, task: Task) -> None:
        self._check_stop()
        ffmpeg, ffprobe = find_tools(self.tool_dir)
        directory = _workdir(task)
        directory.mkdir(parents=True, exist_ok=True)
        if self._recover_commit(task, directory, ffprobe):
            return
        target = self._output(task)
        ids = [task.choice.video_id] + ([task.choice.audio_id] if task.choice.audio_id else [])
        have_inputs = self._have_verified_inputs(task, directory, ffprobe)
        if have_inputs:
            info = {}
            counts = {
                fmt_id: (directory / ("video.media" if index == 0 else "audio.media")).stat().st_size
                for index, fmt_id in enumerate(ids)
            }
            self._set(task, "checking", "已下载输入流校验通过，准备重新封装。")
        else:
            info = self._resolve_selected(task)
            counts = {fmt_id: 0 for fmt_id in ids}
        if have_inputs or task.choice.estimated_bytes:
            # Cached inputs already occupy disk; only the new MP4 needs additional
            # space. Fresh downloads may coexist with another complete MP4.
            input_bytes = sum(counts.values()) if have_inputs else task.choice.estimated_bytes * 2
            needed = input_bytes + 64 * 1024 * 1024
            if shutil.disk_usage(task.output_dir).free < needed:
                raise AppError("磁盘剩余空间不足以同时保存输入流和 MP4。")
        self._check_stop()
        if not have_inputs:
            self._set(task, "downloading", "下载视频流……")
        totals: dict[str, int | None] = {
            fmt_id: counts[fmt_id] if have_inputs else None for fmt_id in ids
        }
        last_update = 0.0
        last_speed = time.monotonic()
        last_bytes = 0
        refreshes = 0

        def notice(message: str):
            task.message = message
            self._publish(task)

        for index, format_id in enumerate([] if have_inputs else ids):
            destination = directory / ("video.media" if index == 0 else "audio.media")
            budget = RetryBudget()

            def progress(size: int, total: int | None, fmt_id=format_id):
                nonlocal last_update, last_speed, last_bytes
                counts[fmt_id], totals[fmt_id] = size, total
                downloaded = sum(counts.values())
                now = time.monotonic()
                if now - last_update < 0.2:
                    return
                elapsed = now - last_speed
                task.speed = max(0.0, (downloaded - last_bytes) / elapsed) if elapsed > 0 else 0.0
                task.downloaded_bytes = downloaded
                task.total_bytes = sum(totals.values()) if all(v is not None for v in totals.values()) else None
                task.eta = ((task.total_bytes - downloaded) / task.speed) if task.total_bytes and task.speed > 0 else None
                task.message = f"正在下载{'视频' if index == 0 else '音频'}流……"
                last_update, last_speed, last_bytes = now, now, downloaded
                self._publish(task, save=False)

            while True:
                resource = self._resource(task, info, format_id)
                try:
                    self.downloader.download(resource, destination, self._stop, progress, notice, budget)
                    counts[format_id] = destination.stat().st_size
                    totals[format_id] = counts[format_id]
                    break
                except RefreshRequired:
                    budget.consume()
                    refreshes += 1
                    if refreshes > 1:
                        raise AppError("重新解析后媒体仍不可访问，请稍后重试或确认访问权限。")
                    notice("媒体地址可能已过期，正在重新解析原格式……")
                    info = self._resolve_selected(task)
            self._set(task, "checking", f"检查{'视频' if index == 0 else '音频'}流……")
            try:
                validate_media(
                    inspect_media(destination, ffprobe, self._stop),
                    video=index == 0, audio=index == 1 or task.choice.audio_id is None,
                    expected_duration=task.part.duration, height=task.choice.height if index == 0 else None,
                )
            except MediaError as exc:
                destination.with_name(destination.name + ".json").unlink(missing_ok=True)
                raise MediaError(str(exc) + " 该流缓存已失效，重试会重新下载该流。") from exc
            self._set(task, "downloading", "输入流已检查，继续处理……")
        self._check_stop()
        task.downloaded_bytes = sum(counts.values())
        task.total_bytes = task.downloaded_bytes
        task.speed, task.eta = 0.0, None
        self._set(task, "merging", "正在无损封装 MP4……")
        temporary = directory / "merged.partial.mp4"

        def mux_progress(fraction: float):
            task.message = f"正在无损封装 MP4：{fraction:.0%}"
            self._publish(task, save=False)

        mux_mp4(ffmpeg, directory / "video.media",
                directory / "audio.media" if task.choice.audio_id else None,
                temporary, task.part.duration, self._stop, mux_progress)
        self._set(task, "verifying", "检查成品音视频和时长……")
        validate_media(inspect_media(temporary, ffprobe, self._stop), video=True, audio=True,
                       expected_duration=task.part.duration, height=task.choice.height)
        receipt = {
            "task_id": task.id, "key": list(_task_key(task)), "target": str(target),
            "size": temporary.stat().st_size, "sha256": digest_file(temporary, self._stop),
        }
        with temporary.open("r+b") as committed_stream:
            os.fsync(committed_stream.fileno())
        atomic_json(directory / "commit.json", receipt)
        self._set(task, "committing", "提交已验证的成品……")
        self._check_stop()
        try:
            _commit_no_overwrite(temporary, target)
        except FileExistsError as exc:
            raise AppError("输出文件名已被其他文件占用；未覆盖该文件，请重试生成新文件名。") from exc
        # Publication is complete: cancellation arriving here cannot undo a valid MP4.
        self._set(task, "completed", "MP4 已生成，包含视频和声音。")
        self._cleanup_inputs(directory)
