"""浏览器登录链的私有实现模块。

包顶层刻意保持为空：登录链会加载 Playwright，而 ``--help``、``--check-config``
和菜单需要在没有浏览器依赖的环境里运行。需要公开接口时请显式导入具体模块，
例如 ``from swu_checkin.auth.flow import get_token``。
"""
