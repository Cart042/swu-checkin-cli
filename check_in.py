#!/usr/bin/env python3
"""兼容入口：保留 ``python check_in.py`` 的用法。

实现已经迁移到 ``src/swu_checkin/`` 包，正式入口是控制台脚本
``swu-checkin``（安装后可用）或 ``python -m swu_checkin``。这个文件只在
源码检出目录里把 ``src`` 加进 ``sys.path``，因此文档、Docker 镜像、
GitHub Actions 和已有部署脚本都可以继续使用同一个命令。

如果不需要这个兼容入口，可以执行 ``pip install -e .`` 后改用
``swu-checkin``；两者运行的是同一份代码。
"""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from swu_checkin.cli import main  # noqa: E402 - 需要先完成 sys.path 设置

if __name__ == "__main__":
    raise SystemExit(main())
