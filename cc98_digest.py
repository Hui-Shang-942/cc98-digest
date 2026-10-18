"""兼容入口：`python cc98_digest.py <子命令>`（等价于 `python -m cc98digest`）。

之所以同时提供，是因为很多人习惯直接运行脚本；模块入口在计划任务里更稳健，
两个入口共用同一份 cli.main()。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意工作目录直接运行本文件
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cc98digest.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
