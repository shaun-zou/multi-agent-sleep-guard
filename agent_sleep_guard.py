#!/usr/bin/env python
"""agent-sleep-guard 命令行入口。

三个可独立调用的入口:
    python agent_sleep_guard.py scan [run|list|show|revise|verify] ...
    python agent_sleep_guard.py monitor --snapshot <快照ID>
    python agent_sleep_guard.py selfcheck

依赖: Python 3.9+ 标准库, 无第三方包。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asg.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
