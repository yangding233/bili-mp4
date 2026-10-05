"""Offscreen checks for safety-critical UI behavior, without network or downloads."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from bili_mp4 import ui
from bili_mp4.domain import FormatChoice, Part, Task
from bili_mp4.store import TaskStore


class FakeManager:
    def __init__(self, store, emit_callback, tool_dir=None):
        self.tool_dir = tool_dir
        self.starts = 0
        self.enqueued = []

    def start(self):
        self.starts += 1

    def enqueue(self, planned, output_dir):
        self.enqueued.extend(planned)
        return []

    def shutdown(self, timeout=2.0):
        return True


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DownloadManager", FakeManager)
    monkeypatch.setattr(ui, "find_tools", lambda directory=None: ("ffmpeg", "ffprobe"))
    main = ui.MainWindow(data_dir=tmp_path)
    yield main
    main.close()
    app.processEvents()


def sample_part():
    return Part("BV12jup6AEVi", "1234", 1, "测试分 P", 60, "")


def sample_choice(height=720):
    return FormatChoice("video1", "audio1", height, 30, "avc1", "mp4a", 1024)


def test_restoring_paused_task_never_starts_download(app, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DownloadManager", FakeManager)
    monkeypatch.setattr(ui, "find_tools", lambda directory=None: ("ffmpeg", "ffprobe"))
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        store.save(Task("restored", sample_part(), sample_choice(), str(tmp_path), status="paused"))
    main = ui.MainWindow(data_dir=tmp_path)
    try:
        assert main.manager.starts == 0
        assert main.queue_table.rowCount() == 1
        assert main.queue_table.item(0, 2).text() == "已暂停"
    finally:
        main.close()
        app.processEvents()


def test_full_download_bytes_do_not_mark_merge_as_completed(window):
    task = Task("progress", sample_part(), sample_choice(), str(window.data_dir),
                status="merging", downloaded_bytes=1024, total_bytes=1024)
    window._manager_event({"type": "task", "task": task.to_dict()})
    progress = window.queue_table.cellWidget(0, 3)
    assert window.queue_table.item(0, 2).text() == "合并"
    assert progress.maximum() == 0
    task.status = "completed"
    window._manager_event({"type": "task", "task": task.to_dict()})
    assert progress.maximum() == 100
    assert progress.value() == 100
    assert progress.format() == "完成"


def test_unavailable_requested_height_does_not_enqueue_or_silently_downgrade(window, monkeypatch):
    part = sample_part()
    window.url_edit.setText(part.url)
    window._worker_result("parts", [part])
    assert window.parts_table.item(0, 0).checkState() == Qt.CheckState.Checked
    window.quality_combo.setCurrentIndex(window.quality_combo.findData(1080))
    window._apply_quality()
    monkeypatch.setattr(ui.resolver, "resolve_formats", lambda part: ({}, [sample_choice(720)]))
    warnings = []
    monkeypatch.setattr(ui.QMessageBox, "warning", lambda parent, title, message: warnings.append(message))
    # Run the real preparation and callbacks synchronously; networking is stubbed.
    monkeypatch.setattr(window, "_run", lambda name, operation: window._worker_result(name, operation()))
    window._enqueue()
    assert not window.manager.enqueued
    assert window.manager.starts == 0
    assert warnings and "1080" in warnings[0]
    assert "未加入任务" in window.statusBar().currentMessage()


def test_diagnostics_hide_signed_urls_and_authorization(window, tmp_path, monkeypatch):
    destination = tmp_path / "diagnostics.txt"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *args: (str(destination), "文本文件 (*.txt)"))
    window._log("网络失败 https://cdn.example/video?deadline=123&token=SECRET")
    window._log("Authorization: Bearer SECRET-TOKEN")
    window._log("Cookie: SESSDATA=SECRET-COOKIE")
    window._export_logs()
    result = destination.read_text(encoding="utf-8")
    assert "SECRET" not in result
    assert "cdn.example" not in result
    assert "已隐藏" in result


def test_exact_format_selection_uses_format_ids_not_list_position(window):
    first = sample_choice(720)
    second = FormatChoice("video2", "audio1", 1080, 60, "avc1", "mp4a", 2048)
    specification = {"kind": "exact", "video_id": first.video_id, "audio_id": first.audio_id}
    combo = window._make_quality_combo([second, first], specification)
    assert combo.currentData() == specification
    assert "720p" in combo.currentText()
