"""登录失败分类与登录链异常。

``FailureReason`` 的取值同时是日志、诊断文件名和
:data:`swu_checkin.status.LOGIN_REASON_STATUS` 的键，因此它继承 ``str``：
既有按字符串比较的调用方不需要修改。

``_RetryableHttpError`` 和围绕 HTTP 状态的几个判定函数集中放在这里，
避免浏览器层和流程层各自解释 4xx/5xx。
"""

from __future__ import annotations

from enum import StrEnum


class FailureReason(StrEnum):
    """机器可读的登录失败原因。"""

    CREDENTIAL = "credential"
    PAGE_LOAD = "page_load"
    WAF_BLOCKED = "waf_blocked"
    CAPTCHA = "captcha"
    TOKEN_EXTRACT = "token_extract"
    LOGIN_PAGE_CHANGED = "login_page_changed"
    UNKNOWN = "unknown"


class LoginError(Exception):
    """A browser login failed for a reason understood by the CLI."""

    def __init__(self, reason: FailureReason | str, message: str) -> None:
        super().__init__(message)
        self.reason: FailureReason | str = reason


class _RetryableHttpError(Exception):
    """An authentication page answered with HTTP 4xx/5xx.

    The school's front end answers intermittently, so callers keep their retry
    budget instead of treating the first error document as fatal.
    """

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _http_status(response):
    """Return the HTTP status of a navigation response, when it carries one."""
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def _require_ok_http_status(status):
    """Do not mistake an HTTP error document for a changed login form."""
    if status is not None and status >= 400:
        # URLs, headers and response bodies may contain authentication state.
        raise _RetryableHttpError(status)


def _remember_http_status(status, previous):
    """Keep the last 4xx/5xx seen while retrying, for the final classification."""
    if isinstance(status, int) and status >= 400:
        return status
    return previous


def _login_failure_reason(last_login_status):
    """Classify a login that never completed.

    ``POST /am/UI/Login`` answering HTTP 400 is the signature of the front-end
    interception described in ``docs/login-troubleshooting.md``; anything else
    that reached the page stays a captcha failure.
    """
    return FailureReason.WAF_BLOCKED if last_login_status == 400 else FailureReason.CAPTCHA
