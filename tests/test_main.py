"""Startup failure handling for console and windowed executable checks."""
import builtins
import io
import sys

import pytest

from bili_mp4.__main__ import main


@pytest.mark.parametrize("windowed", [False, True])
def test_missing_gui_dependency_returns_failure_without_modal_smoke_dialog(monkeypatch, windowed):
    original_import = builtins.__import__

    def fail_ui(name, *args, **kwargs):
        if name == "ui":
            raise ImportError("QtCore DLL unavailable")
        return original_import(name, *args, **kwargs)

    stream = None if windowed else io.StringIO()
    monkeypatch.setattr(sys, "argv", ["bili-mp4", "--smoke-test"])
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(builtins, "__import__", fail_ui)
    assert main() == 1
    if stream is not None:
        assert "QtCore DLL unavailable" in stream.getvalue()
