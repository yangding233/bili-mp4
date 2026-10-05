"""Guard against collecting third-party ICU instead of Windows ICU."""
import importlib.util
import os
from pathlib import Path
import shutil

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows DLL resolution regression")
@pytest.mark.parametrize("build_fails", [False, True])
def test_build_resolves_windows_icu_and_restores_path(tmp_path, monkeypatch, build_fails):
    import PyInstaller.__main__
    from PyInstaller.depend.bindepend import resolve_library_path

    conflicting = tmp_path / "icuuc.dll"
    # A real same-architecture PE file is required: PyInstaller ignores stubs.
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "kernel32.dll", conflicting)
    contaminated_path = str(tmp_path) + os.pathsep + os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", contaminated_path)
    assert Path(resolve_library_path("icuuc.dll")) == conflicting
    script = Path(__file__).parents[1] / "scripts" / "run_pyinstaller.py"
    spec = importlib.util.spec_from_file_location("build_runner", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    def fake_build(arguments):
        assert arguments == ["--version"]
        assert Path(resolve_library_path("icuuc.dll")).samefile(
            Path(os.environ["SystemRoot"]) / "System32" / "icuuc.dll"
        )
        if build_fails:
            raise RuntimeError("build failed")

    monkeypatch.setattr(PyInstaller.__main__, "run", fake_build)
    if build_fails:
        with pytest.raises(RuntimeError, match="build failed"):
            runner.main(["--version"])
    else:
        runner.main(["--version"])
    assert os.environ["PATH"] == contaminated_path
