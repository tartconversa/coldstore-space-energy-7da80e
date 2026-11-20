#!/usr/bin/env python3
"""隔离模式启动器：python3 -I run_coldstore.py <coldstore 子命令> ...

-I 不加载用户站点包、不读 PYTHONPATH/环境变量，防止资料目录中的同名模块被导入。
本脚本把仓库根目录显式插入 sys.path 后转交 coldstore CLI。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coldstore.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
