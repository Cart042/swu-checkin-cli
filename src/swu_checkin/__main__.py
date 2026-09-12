"""``python -m swu_checkin`` 入口，与 ``swu-checkin`` 控制台脚本等价。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
