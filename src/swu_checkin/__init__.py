"""西南大学自动打卡 CLI。

包内按职责分层：

``swu_checkin.cli``
    命令行入口、配置检查、运行锁和结果汇总。
``swu_checkin.config`` / ``swu_checkin.menu``
    账号来源、本地配置文件和数字菜单；不导入浏览器或 HTTP 运行时。
``swu_checkin.api.school``
    学校接口的 HTTP 传输层，可在没有浏览器的环境下单独测试。
``swu_checkin.auth``
    浏览器登录链：状态机（``flow``）、页面操作（``browser``）、验证码（``captcha``）、
    Cookie（``cookies``）、Token（``tokens``）和页面文案判定（``pages``）。
``swu_checkin.checkin_service`` / ``swu_checkin.runner``
    单账号签到流程与多账号并发、重试和汇总。
``swu_checkin.notify``
    多通道推送。
``swu_checkin.logging_utils``
    日志初始化，供 CLI 与性能测量脚本共用。

这些子模块刻意不在包顶层导入：``--help``、``--check-config`` 和菜单必须在
没有安装 Playwright、ddddocr 的环境里也能运行。
"""

from __future__ import annotations

__version__ = "1.2.0"

__all__ = ["__version__"]
