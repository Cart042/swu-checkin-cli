"""跨模块共享的数据结构。

这些信息过去以裸 ``dict``、位置相关的元组和魔法字符串在模块之间传递。
这里用 ``TypedDict`` / ``dataclass`` 明确表达字段含义，让静态检查和后来者
都能直接看懂接口形状。

登录链的失败信息不需要单独的 ``LoginResult``：登录失败一律以
:class:`swu_checkin.auth.errors.LoginError`（带 ``FailureReason``）抛出，
成功时返回 Token 字符串，因此一个只包装字符串的结果对象不会带来额外信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from .status import CheckinStatus


class Account(TypedDict):
    """一个校园网账号；字段名与 ``users.json`` / ``SWU_USERS`` 一致。"""

    username: str
    password: str


# Token 缓存是「账号 -> Token」的映射，键始终是字符串形式的账号。
TokenCache = dict[str, str]


@dataclass(frozen=True)
class CheckinResult:
    """单个账号在一次运行中的最终结果。

    取代原先的 ``(message, ok, attempt)`` 三元组，避免调用方按位置取值。
    ``status`` 在“运行结束时仍未完成”的情况下为 ``None``：那种情况只有
    放弃原因，没有可报告的状态码。
    """

    username: str
    message: str
    ok: bool
    attempt: int = 1
    status: CheckinStatus | None = None


@dataclass(frozen=True)
class NotificationResult:
    """一次推送的结果汇总。

    ``bool(result)`` 表示是否至少有一个通道发送成功，因此既有的布尔用法
    （``if send_push(...)``）不需要修改。
    """

    sent: bool
    channels: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.sent
