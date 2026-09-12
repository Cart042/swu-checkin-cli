"""Shared status codes and status text for the CLI, runner and check-in service.

状态码统一用 :class:`CheckinStatus` 表达，替代散落在各模块的魔法数字。
``IntEnum`` 的成员与原来的整数完全兼容（比较、哈希、字典键和格式化输出都
一样），因此日志、推送汇总和已有调用方不需要修改。
"""

from __future__ import annotations

from enum import IntEnum

from .auth.errors import FailureReason


class CheckinStatus(IntEnum):
    """一个账号的签到结果状态码。"""

    NO_TASK = 0
    SUCCESS = 1
    ALREADY_CHECKED_IN = 2
    CREDENTIAL_FAILED = 3
    CONNECTION_ERROR = 4
    ON_LEAVE = 5
    PAGE_LOAD_FAILED = 6
    CAPTCHA_FAILED = 7
    TOKEN_EXTRACT_FAILED = 8
    LOGIN_PAGE_CHANGED = 9
    SCHOOL_API_ERROR = 10
    TOKEN_INVALID = 11


def coerce_status(value) -> CheckinStatus:
    """Return *value* as a status code, falling back to ``SCHOOL_API_ERROR``.

    The runner accepts an injected ``checkin_func``, so an unknown value must be
    reported as a school-side failure instead of raising inside the summary loop.
    """

    try:
        return CheckinStatus(value)
    except (TypeError, ValueError):
        return CheckinStatus.SCHOOL_API_ERROR


# 键类型显式包含 ``int``：日志、推送汇总和历史调用方都按整数状态码取文案。
STATUS_MESSAGES: dict[CheckinStatus | int, str] = {
    CheckinStatus.NO_TASK: "今日暂无签到任务。",
    CheckinStatus.SUCCESS: "签到成功。",
    CheckinStatus.ALREADY_CHECKED_IN: "今日已签到，无需重复操作。",
    CheckinStatus.CREDENTIAL_FAILED: "账号或密码验证失败，请检查后重试。",
    CheckinStatus.CONNECTION_ERROR: "连接错误或请求超时，请稍后重试。",
    CheckinStatus.ON_LEAVE: "请假中，请检查是否有打卡任务。",
    CheckinStatus.PAGE_LOAD_FAILED: "登录页加载失败或超时，可能是学校服务或网络异常。",
    CheckinStatus.CAPTCHA_FAILED: "验证码连续识别失败，请稍后重试或使用 --force-login。",
    CheckinStatus.TOKEN_EXTRACT_FAILED: "登录成功但 Token 提取失败，可能是页面结构变化。",
    CheckinStatus.LOGIN_PAGE_CHANGED: "学校登录页结构可能变化，请更新脚本选择器。",
    CheckinStatus.SCHOOL_API_ERROR: "学校接口返回异常，可能是服务暂时不可用。",
    CheckinStatus.TOKEN_INVALID: "Token 校验失败或已失效，请尝试 --force-login。",
}

# 键类型显式包含 ``str``：``FailureReason`` 是 ``str`` 枚举，历史调用方的
# ``LOGIN_REASON_STATUS["captcha"]`` 必须继续有效。
LOGIN_REASON_STATUS: dict[FailureReason | str, CheckinStatus] = {
    FailureReason.CREDENTIAL: CheckinStatus.CREDENTIAL_FAILED,
    FailureReason.PAGE_LOAD: CheckinStatus.PAGE_LOAD_FAILED,
    # WAF 拦截同样表现为登录页加载失败，两者共用状态码 6。
    FailureReason.WAF_BLOCKED: CheckinStatus.PAGE_LOAD_FAILED,
    FailureReason.CAPTCHA: CheckinStatus.CAPTCHA_FAILED,
    FailureReason.TOKEN_EXTRACT: CheckinStatus.TOKEN_EXTRACT_FAILED,
    FailureReason.LOGIN_PAGE_CHANGED: CheckinStatus.LOGIN_PAGE_CHANGED,
}
