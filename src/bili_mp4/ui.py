"""Chinese Windows desktop interface for Bili MP4."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QUrl, QObject, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget, QHeaderView, QAbstractItemView,
)

from . import __version__, resolver
from .engine import DownloadManager, find_tools
from .store import TaskStore

STATUS_LABELS = {
    "queued": "等待", "pending": "等待", "resolving": "解析", "downloading": "下载",
    "paused": "已暂停", "pausing": "正在暂停", "verifying": "校验", "merging": "合并",
    "validating": "验证", "checking": "校验", "committing": "保存成品", "completed": "完成", "complete": "完成", "failed": "失败",
    "cancelled": "已取消", "canceled": "已取消", "stopped": "已停止",
}
COMPLETE = {"completed", "complete"}
TERMINAL = COMPLETE | {"failed", "cancelled", "canceled", "paused", "stopped"}


def clean_message(value: object) -> str:
    """Never display or export signed playback URLs or authorization data."""
    result = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(value))
    result = re.sub(r"https?://[^\s<>\"']+", "[网络地址已隐藏]", result, flags=re.I)
    result = re.sub(r"(?i)\b(cookie|authorization|token|sessdata|bili_jct)\s*[:=]\s*[^\r\n]+", r"\1=[已隐藏]", result)
    return result[:4000]


def human_bytes(value: float | int | None) -> str:
    if value is None:
        return "未知"
    size = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return "未知"


def human_duration(value: float | int | None) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def choice_label(choice: Any) -> str:
    height = getattr(choice, "height", None)
    fps = getattr(choice, "fps", None)
    video = str(getattr(choice, "vcodec", "未知"))
    audio = str(getattr(choice, "acodec", "未知"))
    quality = f"{height}p" if height else "原始分辨率"
    if fps:
        quality += f" / {float(fps):g} fps"
    return f"{quality} · {video} + {audio}"


def default_data_dir() -> Path:
    configured = os.environ.get("BILI_MP4_DATA_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "BiliMP4" if base else Path.home() / ".bili-mp4"


class Bridge(QObject):
    result = Signal(str, object)
    error = Signal(str, str)
    event = Signal(object)


class MainWindow(QMainWindow):
    """Widgets own no network work; daemon workers report through Qt signals."""

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"Bili MP4 v{__version__} · 下载与无损合并")
        self.resize(1180, 840)
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "settings.json"
        self.settings = self._read_settings()
        self.parts: list[Any] = []
        self.formats: dict[str, list[Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.task_rows: dict[str, int] = {}
        self.logs: list[str] = []
        self.busy: set[str] = set()
        self.closing = False
        self.bridge = Bridge(self)
        self.bridge.result.connect(self._worker_result)
        self.bridge.error.connect(self._worker_error)
        self.store = TaskStore(self.data_dir / "tasks.sqlite3")
        self.manager = DownloadManager(self.store, self.bridge.event.emit,
                                       tool_dir=self.settings.get("tool_dir"))
        self._build_ui()
        self.bridge.event.connect(self._manager_event)
        self._apply_style()
        self._restore_tasks()
        self._refresh_tools()
        self.statusBar().showMessage("就绪 · 仅下载你有权访问的内容")

    def _read_settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        self.settings["output_dir"] = self.output_edit.text().strip()
        temporary = self.config_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.config_file)
        except OSError as exc:
            self._log(f"保存设置失败：{exc}")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        title = QLabel("Bili MP4")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("选择分 P 与清晰度，下载后自动合并为带声音的 MP4。"))

        source = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴 B 站视频链接或 BV 号，例如 BV12jup6AEVi")
        self.url_edit.returnPressed.connect(self._parse)
        self.parse_button = QPushButton("解析视频")
        self.parse_button.setObjectName("primary")
        self.parse_button.clicked.connect(self._parse)
        source.addWidget(self.url_edit, 1)
        source.addWidget(self.parse_button)
        layout.addLayout(source)

        self.video_label = QLabel("尚未解析视频")
        self.video_label.setWordWrap(True)
        layout.addWidget(self.video_label)
        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        part_actions = QHBoxLayout()
        self.select_all = QCheckBox("全选分 P")
        self.select_all.toggled.connect(self._select_all)
        part_actions.addWidget(self.select_all)
        part_actions.addStretch(1)
        part_actions.addWidget(QLabel("应用到勾选分 P："))
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("兼容优先，同编码最高", None)
        for height in (2160, 1440, 1080, 720, 480, 360):
            self.quality_combo.addItem(f"{height}p（需要可用）", height)
        part_actions.addWidget(self.quality_combo)
        apply_quality = QPushButton("应用清晰度")
        apply_quality.clicked.connect(self._apply_quality)
        part_actions.addWidget(apply_quality)
        self.formats_button = QPushButton("查看所选格式")
        self.formats_button.clicked.connect(self._fetch_formats)
        part_actions.addWidget(self.formats_button)
        top_layout.addLayout(part_actions)
        self.parts_table = QTableWidget(0, 5)
        self.parts_table.setHorizontalHeaderLabels(["选择", "分 P", "标题", "时长", "清晰度 / 编码"])
        self.parts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.parts_table.verticalHeader().hide()
        self.parts_table.setAlternatingRowColors(True)
        self.parts_table.setColumnWidth(0, 52)
        self.parts_table.setColumnWidth(1, 60)
        self.parts_table.setColumnWidth(3, 75)
        self.parts_table.setColumnWidth(4, 360)
        self.parts_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        top_layout.addWidget(self.parts_table)
        note = QLabel("每个 P 独立输出。格式按实际访问权限获取；指定清晰度不可用时会提示，不自动降级。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        top_layout.addWidget(note)
        output = QHBoxLayout()
        output.addWidget(QLabel("保存位置"))
        self.output_edit = QLineEdit(self.settings.get("output_dir", str(Path.home() / "Downloads" / "BiliMP4")))
        output.addWidget(self.output_edit, 1)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._choose_output)
        output.addWidget(browse)
        self.enqueue_button = QPushButton("加入队列并下载")
        self.enqueue_button.setObjectName("primary")
        self.enqueue_button.setEnabled(False)
        self.enqueue_button.clicked.connect(self._enqueue)
        output.addWidget(self.enqueue_button)
        top_layout.addLayout(output)
        splitter.addWidget(top)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        queue_actions = QHBoxLayout()
        queue_actions.addWidget(QLabel("下载队列"))
        queue_actions.addStretch(1)
        for text, slot in (("暂停", self._pause), ("继续", self._resume),
                           ("取消", self._cancel), ("重试", self._retry),
                           ("打开输出目录", self._open_folder)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            queue_actions.addWidget(button)
        bottom_layout.addLayout(queue_actions)
        self.queue_table = QTableWidget(0, 8)
        self.queue_table.setHorizontalHeaderLabels(["分 P / 标题", "清晰度", "阶段", "进度", "速度", "剩余", "状态说明", "输出文件"])
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.verticalHeader().hide()
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setColumnWidth(0, 190)
        self.queue_table.setColumnWidth(1, 75)
        self.queue_table.setColumnWidth(2, 75)
        self.queue_table.setColumnWidth(3, 145)
        self.queue_table.setColumnWidth(4, 90)
        self.queue_table.setColumnWidth(5, 65)
        self.queue_table.setColumnWidth(7, 160)
        self.queue_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.queue_table.cellDoubleClicked.connect(self._open_file)
        bottom_layout.addWidget(self.queue_table)
        splitter.addWidget(bottom)
        splitter.setSizes([310, 300])
        layout.addWidget(splitter, 1)

        tools = QHBoxLayout()
        self.tools_label = QLabel()
        self.tools_label.setObjectName("muted")
        tools.addWidget(self.tools_label, 1)
        self.tools_button = QPushButton("设置 FFmpeg 目录")
        self.tools_button.clicked.connect(self._choose_tools)
        tools.addWidget(self.tools_button)
        export = QPushButton("导出诊断日志")
        export.clicked.connect(self._export_logs)
        tools.addWidget(export)
        layout.addLayout(tools)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(90)
        self.log_view.setPlaceholderText("任务提示与诊断信息（网络地址和授权信息会隐藏）")
        layout.addWidget(self.log_view)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f5f7fb; color: #182335; font-size: 13px; }
            QLabel#heading { font-size: 27px; font-weight: 700; color: #1768d5; }
            QLabel#muted { color: #64748b; font-size: 12px; }
            QLineEdit, QComboBox, QPlainTextEdit { background: white; border: 1px solid #d7e0ec; border-radius: 5px; padding: 7px; }
            QPushButton { background: white; border: 1px solid #d7e0ec; border-radius: 5px; padding: 7px 11px; }
            QPushButton:hover { background: #eaf2ff; border-color: #7aaaf0; }
            QPushButton#primary { background: #1768d5; color: white; border-color: #1768d5; font-weight: 600; }
            QPushButton#primary:hover { background: #145bb9; }
            QPushButton:disabled { color: #98a5b5; background: #e7ecf3; border-color: #e7ecf3; }
            QTableWidget { background: white; alternate-background-color: #f7f9fc; border: 1px solid #d7e0ec; gridline-color: #edf1f6; selection-background-color: #dceaff; selection-color: #172d4d; }
            QHeaderView::section { background: #edf3fb; padding: 7px; border: 0; border-bottom: 1px solid #d7e0ec; font-weight: 600; }
            QProgressBar { background: #eef2f8; border: 0; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background: #62a2ef; border-radius: 4px; }
        """)

    @staticmethod
    def _part_key(part: Any) -> str:
        return str(getattr(part, "cid", None) or getattr(part, "index"))

    def _run(self, name: str, operation: Callable[[], Any]) -> None:
        if name in self.busy:
            return
        self.busy.add(name)
        self._set_busy()
        def work() -> None:
            try:
                result = operation()
            except Exception as exc:
                if not self.closing:
                    self.bridge.error.emit(name, clean_message(exc))
            else:
                if not self.closing:
                    self.bridge.result.emit(name, result)
        threading.Thread(target=work, name=f"bili-ui-{name}", daemon=True).start()

    def _set_busy(self) -> None:
        occupied = bool(self.busy)
        self.parse_button.setEnabled(not occupied)
        self.formats_button.setEnabled(bool(self.parts) and not occupied)
        self.enqueue_button.setEnabled(bool(self.parts) and not occupied)
        self.parts_table.setEnabled(not occupied)
        self.url_edit.setEnabled(not occupied)
        self.quality_combo.setEnabled(not occupied)
        self.select_all.setEnabled(not occupied)

    def _parse(self) -> None:
        if self.busy:
            return
        source = self.url_edit.text().strip()
        if not source:
            QMessageBox.information(self, "输入链接", "请先输入 BV 号或 B 站视频链接。")
            return
        self.statusBar().showMessage("正在解析分 P…")
        self._run("parts", lambda: resolver.resolve_parts(source))

    def _select_all(self, checked: bool) -> None:
        for row in range(self.parts_table.rowCount()):
            self.parts_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _selected_parts(self) -> list[tuple[int, Any]]:
        return [(row, part) for row, part in enumerate(self.parts)
                if self.parts_table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def _make_quality_combo(self, choices: list[Any] | None = None,
                            previous: dict[str, Any] | None = None) -> QComboBox:
        combo = QComboBox()
        combo.addItem("兼容优先，同编码最高（H.264 / AAC）", {"kind": "auto"})
        for height in (2160, 1440, 1080, 720, 480, 360):
            available = not choices or any(getattr(item, "height", None) == height for item in choices)
            label = f"{height}p" + (" · 当前不可用" if not available else "")
            combo.addItem(label, {"kind": "height", "height": height})
            if choices and not available:
                combo.model().item(combo.count() - 1).setEnabled(False)
        for index, choice in enumerate(choices or []):
            combo.addItem(choice_label(choice), {"kind": "exact", "video_id": choice.video_id, "audio_id": choice.audio_id})
        if previous:
            for index in range(combo.count()):
                if combo.itemData(index) == previous:
                    combo.setCurrentIndex(index)
                    break
        return combo

    def _apply_quality(self) -> None:
        height = self.quality_combo.currentData()
        requested = {"kind": "auto"} if height is None else {"kind": "height", "height": height}
        for row, _ in self._selected_parts():
            combo = self.parts_table.cellWidget(row, 4)
            for index in range(combo.count()):
                if combo.itemData(index) == requested:
                    combo.setCurrentIndex(index)
                    break

    def _fetch_formats(self) -> None:
        selected = self._selected_parts()
        if not selected:
            QMessageBox.information(self, "选择分 P", "请至少勾选一个分 P。")
            return
        self.statusBar().showMessage("正在获取所选分 P 的实际格式…")
        def fetch() -> list[tuple[str, Any, list[Any], str | None]]:
            records = []
            for _, part in selected:
                try:
                    _, choices = resolver.resolve_formats(part)
                    records.append((self._part_key(part), part, choices, None))
                except Exception as exc:
                    records.append((self._part_key(part), part, [], clean_message(exc)))
            return records
        self._run("formats", fetch)

    def _enqueue(self) -> None:
        selected = self._selected_parts()
        if not selected:
            QMessageBox.information(self, "选择分 P", "请至少勾选一个分 P。")
            return
        output = self.output_edit.text().strip()
        if not output:
            QMessageBox.information(self, "保存位置", "请选择输出目录。")
            return
        self._save_settings()
        records = [(part, dict(self.parts_table.cellWidget(row, 4).currentData()),
                    self.formats.get(self._part_key(part))) for row, part in selected]
        self.statusBar().showMessage("正在校验各分 P 的清晰度…")
        def prepare() -> dict[str, Any]:
            planned = []
            updated = []
            errors = []
            for part, specification, cached in records:
                try:
                    choices = cached
                    if choices is None:
                        _, choices = resolver.resolve_formats(part)
                    updated.append((self._part_key(part), part, choices, None))
                    if specification["kind"] == "exact":
                        matching = [item for item in choices if item.video_id == specification["video_id"] and item.audio_id == specification["audio_id"]]
                        if not matching:
                            raise ValueError("原先选定的格式已不可用，请重新获取格式。")
                        choice = matching[0]
                    else:
                        choice = resolver.select_choice(choices, height=specification.get("height"))
                    planned.append((part, choice))
                except Exception as exc:
                    errors.append(f"P{getattr(part, 'index')}：{clean_message(exc)}")
            return {"planned": planned, "formats": updated, "errors": errors, "output": output}
        self._run("prepare", prepare)

    def _worker_result(self, name: str, result: Any) -> None:
        self.busy.discard(name)
        if self.closing:
            return
        if name == "parts":
            self.parts = list(result)
            self.formats.clear()
            self.parts_table.setRowCount(len(self.parts))
            self.select_all.blockSignals(True)
            self.select_all.setChecked(False)
            self.select_all.blockSignals(False)
            match = re.search(r"(?:[?&])p=(\d+)", self.url_edit.text())
            preferred = int(match.group(1)) if match else 1
            for row, part in enumerate(self.parts):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked if getattr(part, "index") == preferred else Qt.CheckState.Unchecked)
                self.parts_table.setItem(row, 0, check)
                self.parts_table.setItem(row, 1, QTableWidgetItem(f"P{getattr(part, 'index')}"))
                self.parts_table.setItem(row, 2, QTableWidgetItem(str(getattr(part, "title"))))
                self.parts_table.setItem(row, 3, QTableWidgetItem(human_duration(getattr(part, "duration", None))))
                self.parts_table.setCellWidget(row, 4, self._make_quality_combo())
            self.video_label.setText(f"已解析 {len(self.parts)} 个分 P · 可分别选择实际可用格式")
            self.statusBar().showMessage("解析完成")
        elif name == "formats":
            failures = self._accept_formats(result)
            self.statusBar().showMessage("格式获取完成" if not failures else "部分分 P 格式获取失败")
            if failures:
                QMessageBox.warning(self, "格式获取失败", "\n".join(failures))
        elif name == "prepare":
            self._accept_formats(result["formats"])
            if result["errors"]:
                QMessageBox.warning(self, "无法加入队列", "以下分 P 未通过校验，请调整选择后重试：\n\n" + "\n".join(result["errors"]))
                self.statusBar().showMessage("清晰度校验失败，未加入任务")
            else:
                planned, output = result["planned"], result["output"]
                self._run("enqueue", lambda: self.manager.enqueue(planned, Path(output)))
        elif name == "enqueue":
            for task in result:
                self._update_task(task.to_dict() if hasattr(task, "to_dict") else task)
            self.manager.start()
            self.statusBar().showMessage("任务已加入队列")
        self._set_busy()

    def _accept_formats(self, records: list[Any]) -> list[str]:
        failures = []
        for key, part, choices, error in records:
            if error:
                message = f"P{getattr(part, 'index')}：{error}"
                self._log(message)
                failures.append(message)
                continue
            self.formats[key] = choices
            for row, item in enumerate(self.parts):
                if self._part_key(item) == key:
                    old = self.parts_table.cellWidget(row, 4).currentData()
                    self.parts_table.setCellWidget(row, 4, self._make_quality_combo(choices, old))
                    break
        return failures

    def _worker_error(self, name: str, message: str) -> None:
        self.busy.discard(name)
        if self.closing:
            return
        self._set_busy()
        self._log(message)
        self.statusBar().showMessage("操作失败")
        QMessageBox.warning(self, "操作失败", message)

    def _restore_tasks(self) -> None:
        try:
            for task in self.store.load_all():
                self._update_task(task.to_dict() if hasattr(task, "to_dict") else task)
        except Exception as exc:
            self._log(f"读取历史任务失败：{exc}")

    def _manager_event(self, event: dict[str, Any]) -> None:
        if self.closing:
            return
        if event.get("type") == "task":
            self._update_task(event["task"])
        elif event.get("type") == "log":
            self._log(event.get("message", ""))

    def _update_task(self, task: dict[str, Any]) -> None:
        identity = str(task["id"])
        previous = self.tasks.get(identity)
        self.tasks[identity] = task
        if identity not in self.task_rows:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self.task_rows[identity] = row
            progress = QProgressBar()
            progress.setMinimumHeight(23)
            self.queue_table.setCellWidget(row, 3, progress)
        row = self.task_rows[identity]
        part = task.get("part", {})
        choice = task.get("choice", {})
        status = str(task.get("status", "queued"))
        message = clean_message(task.get("message", ""))
        output = str(task.get("output_file") or "")
        first = QTableWidgetItem(f"P{part.get('index', '?')} · {part.get('title', '')}")
        first.setData(Qt.ItemDataRole.UserRole, identity)
        self.queue_table.setItem(row, 0, first)
        height = choice.get("height", task.get("height"))
        self.queue_table.setItem(row, 1, QTableWidgetItem(f"{height}p" if height else "—"))
        self.queue_table.setItem(row, 2, QTableWidgetItem(STATUS_LABELS.get(status, status)))
        speed = float(task.get("speed") or 0)
        self.queue_table.setItem(row, 4, QTableWidgetItem(human_bytes(speed) + "/s" if speed > 0 and status == "downloading" else "—"))
        self.queue_table.setItem(row, 5, QTableWidgetItem(human_duration(task.get("eta")) if status == "downloading" else "—"))
        self.queue_table.setItem(row, 6, QTableWidgetItem(message))
        item = QTableWidgetItem(Path(output).name if output else "—")
        item.setToolTip(output)
        self.queue_table.setItem(row, 7, item)
        done = int(task.get("downloaded_bytes") or 0)
        total = task.get("total_bytes")
        progress = self.queue_table.cellWidget(row, 3)
        if status in COMPLETE:
            progress.setRange(0, 100)
            progress.setValue(100)
            progress.setFormat("完成")
        elif status in {"merging", "checking", "verifying", "validating", "resolving", "committing"} or (status == "downloading" and not total):
            progress.setRange(0, 0)
            progress.setFormat(STATUS_LABELS.get(status, status))
        else:
            progress.setRange(0, 100)
            percentage = min(99, int(done / total * 100)) if total else 0
            progress.setValue(percentage)
            progress.setFormat(f"{percentage}%")
        progress.setToolTip(f"{human_bytes(done)} / {human_bytes(total)}")
        if status == "failed" and (previous is None or previous.get("status") != "failed"):
            self._log(f"P{part.get('index', '?')} 失败：{message}")

    def _selected_task_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self.queue_table.selectedIndexes()})
        return [str(self.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole)) for row in rows]

    def _pause(self) -> None:
        for identity in self._selected_task_ids():
            self.manager.pause(identity)

    def _resume(self) -> None:
        for identity in self._selected_task_ids():
            self.manager.resume(identity)
        self.manager.start()

    def _cancel(self) -> None:
        for identity in self._selected_task_ids():
            self.manager.cancel(identity)

    def _retry(self) -> None:
        for identity in self._selected_task_ids():
            self.manager.retry(identity)
        self.manager.start()

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 MP4 保存目录", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)
            self._save_settings()

    def _refresh_tools(self) -> None:
        try:
            find_tools(self.settings.get("tool_dir"))
        except Exception as exc:
            self.tools_label.setText("未找到 FFmpeg / ffprobe：请设置包含两个程序的目录")
            self.tools_label.setToolTip(clean_message(exc))
        else:
            self.tools_label.setText("FFmpeg / ffprobe 已就绪 · 无损封装，不重新编码")

    def _choose_tools(self) -> None:
        if any(task.get("status") in {"downloading", "checking", "merging", "verifying", "validating", "committing"} for task in self.tasks.values()):
            QMessageBox.information(self, "工具目录", "请先暂停正在执行的任务，再修改工具目录。")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择包含 ffmpeg.exe 和 ffprobe.exe 的目录", self.settings.get("tool_dir", ""))
        if not folder:
            return
        try:
            find_tools(folder)
        except Exception as exc:
            QMessageBox.warning(self, "媒体工具未就绪", clean_message(exc))
            return
        self.settings["tool_dir"] = folder
        self.manager.tool_dir = folder
        self._save_settings()
        self._refresh_tools()

    def _open_folder(self) -> None:
        selected = self._selected_task_ids()
        output = self.tasks[selected[0]].get("output_file") if selected else None
        folder = Path(output).parent if output else Path(self.output_edit.text())
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))
        else:
            QMessageBox.information(self, "输出目录", "目录尚未创建，请先完成下载或选择已存在的保存位置。")

    def _open_file(self, row: int, column: int) -> None:
        identity = str(self.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        task = self.tasks[identity]
        output = task.get("output_file")
        if task.get("status") in COMPLETE and output and Path(output).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(output).resolve())))

    def _log(self, message: object) -> None:
        line = time.strftime("%H:%M:%S") + " " + clean_message(message)
        self.logs.append(line)
        self.logs = self.logs[-2000:]
        self.log_view.appendPlainText(line)
        self.log_view.document().setMaximumBlockCount(2000)

    def _export_logs(self) -> None:
        suggested = str(Path(self.output_edit.text()) / "bili-mp4-diagnostics.txt")
        destination, _ = QFileDialog.getSaveFileName(self, "导出脱敏诊断日志", suggested, "文本文件 (*.txt)")
        if not destination:
            return
        summaries = [f"P{task.get('part', {}).get('index', '?')} {task.get('status', '')}: {clean_message(task.get('message', ''))}" for task in self.tasks.values()]
        content = "Bili MP4 诊断日志\n已隐藏网络地址与授权信息。\n\n" + "\n".join(summaries + [""] + self.logs)
        try:
            Path(destination).write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "日志导出失败", clean_message(exc))
        else:
            self.statusBar().showMessage("诊断日志已导出")

    def closeEvent(self, event: Any) -> None:
        self.closing = True
        self._save_settings()
        try:
            stopped = self.manager.shutdown(timeout=2.0)
            if stopped:
                self.store.close()
        except Exception as exc:
            self._log(f"关闭任务管理器失败：{exc}")
        event.accept()


def run(*, data_dir: Path | None = None, smoke_test: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("BiliMP4")
    app.setOrganizationName("BiliMP4")
    window = MainWindow(data_dir=data_dir)
    window.show()
    if smoke_test:
        QTimer.singleShot(300, window.close)
        QTimer.singleShot(500, app.quit)
    return app.exec()
