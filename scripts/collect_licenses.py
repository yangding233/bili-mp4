"""Collect installed dependency license texts for a portable Windows build."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys


def collect(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in sorted(
        importlib.metadata.distributions(),
        key=lambda item: (item.metadata.get("Name") or "").lower(),
    ):
        name = distribution.metadata.get("Name") or "unknown"
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        copied = []
        for entry in distribution.files or ():
            relative = Path(str(entry))
            if ".." in relative.parts or relative.is_absolute():
                continue
            parts = [part.lower() for part in relative.parts]
            basename = relative.name.lower()
            is_notice = (
                any(part in {"licenses", "license"} for part in parts)
                or basename.startswith(("license", "copying", "notice", "authors"))
            )
            if not is_notice:
                continue
            source = Path(distribution.locate_file(entry))
            if not source.is_file():
                continue
            target = destination / safe_name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative.as_posix())
        inventory.append(
            {
                "name": name,
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_metadata": distribution.metadata.get("License"),
                "project_urls": distribution.metadata.get_all("Project-URL") or [],
                "copied_license_files": copied,
            }
        )

    python_candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ]
    python_license = next((path for path in python_candidates if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("Python license text was not found in the build interpreter.")
    (destination / "Python").mkdir(exist_ok=True)
    shutil.copy2(python_license, destination / "Python" / python_license.name)
    (destination / "dependency-inventory.json").write_text(
        json.dumps(
            {
                "python_version": sys.version,
                "note": "Installed build-environment packages; not a claim that every package is bundled.",
                "distributions": inventory,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: collect_licenses.py OUTPUT_DIRECTORY")
    collect(Path(sys.argv[1]))
