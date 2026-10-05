"""PyInstaller entry point; keep package-relative imports in bili_mp4."""

from bili_mp4.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
