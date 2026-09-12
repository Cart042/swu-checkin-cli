"""离线测试包。

实现位于 ``src/swu_checkin/``。源码检出目录里没有安装这个包，因此这里把
``src`` 加入 ``sys.path``，让 ``python -m unittest discover -s tests -t .``
在未执行 ``pip install -e .`` 时也能运行同一份代码。
"""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
