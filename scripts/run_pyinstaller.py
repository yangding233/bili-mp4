"""Build with a minimal native DLL search path inside the Python process."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main(arguments: list[str] | None = None) -> None:
    from PyInstaller.__main__ import run

    system_root = Path(os.environ["SystemRoot"])
    previous_path = os.environ.get("PATH")
    try:
        # Set this after Python starts: launchers may augment the child PATH.
        # Qt depends on Windows ICU; a same-named DLL from Poppler/Conda can
        # expose different exports and silently poison dependency collection.
        os.environ["PATH"] = os.pathsep.join(
            str(path) for path in (
                Path(sys.executable).parent,
                Path(sys.base_prefix),
                system_root / "System32",
                system_root,
            )
        )
        run(sys.argv[1:] if arguments is None else arguments)
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


if __name__ == "__main__":
    main()
