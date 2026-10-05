"""Run the Windows desktop application: python -m bili_mp4."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Bili MP4 · B 站视频下载与无损合并")
    parser.add_argument("--data-dir", type=Path, help="任务数据库和设置的保存目录")
    parser.add_argument("--smoke-test", action="store_true", help="启动后自动退出，仅用于界面启动检查")
    args = parser.parse_args()
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from .ui import run
    except ImportError as exc:
        sys.stderr.write(f"无法启动 Bili MP4，缺少运行依赖：{exc}\n请安装项目依赖后重试。\n")
        return 1
    return run(data_dir=args.data_dir, smoke_test=args.smoke_test)


if __name__ == "__main__":
    raise SystemExit(main())
