"""Playwright 浏览器会话与登录页面操作。

这里只负责「打开页面、定位元素、点击、读取验证码图片和页面提示」，不决定登录
策略：流程与状态机在 :mod:`swu_checkin.auth.flow`，Cookie 在
:mod:`swu_checkin.auth.cookies`，页面文案与选择器常量在
:mod:`swu_checkin.auth.pages`，HTTP 状态判定在 :mod:`swu_checkin.auth.errors`。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import urllib.parse
from contextlib import contextmanager

from ..api.school import (
    DeadlineExceeded,
    _redact_text,
    _redact_url,
    _remaining_seconds,
    _timeout_with_deadline,
)
from .debug import _debug_user_id, save_login_debug_artifacts
from .errors import FailureReason, LoginError, _http_status, _remember_http_status
from .pages import (
    _LOGIN_ERROR_MESSAGE_JS,
    _LOGIN_ERROR_MESSAGE_SELECTOR,
    _LOGIN_FAILURE_HINTS,
    BACK_TO_LOGIN_LINK_SELECTOR,
    CAPTCHA_IMAGE_SELECTOR,
    CAPTCHA_INPUT_SELECTOR,
    LOGIN_NAME_SELECTOR,
    PASSWORD_SELECTOR,
    SUBMIT_BUTTON_SELECTOR,
    UNIFIED_LOGIN_BUTTON_SELECTOR,
    USERNAME_PASSWORD_TAB_SELECTOR,
    is_captcha_resource,
    is_device_verification_failure,
)

logger = logging.getLogger("swu")


# Cold logins start Chromium and load the captcha model.  Keep that expensive
# path bounded to one process while cached-token/API work remains concurrent.
_browser_login_semaphore = threading.BoundedSemaphore(1)


def _acquire_browser_login(deadline=None):
    """Acquire a cold-login slot without waiting past ``deadline``."""
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        acquired = _browser_login_semaphore.acquire()
    else:
        acquired = _browser_login_semaphore.acquire(timeout=max(0.0, remaining))
    if not acquired:
        raise DeadlineExceeded("等待浏览器登录并发槽位已超过截止时间")
    try:
        # A slot can become available at the same instant the timeout expires.
        # Do not start a browser after the caller's deadline in that case.
        _remaining_seconds(deadline)
    except Exception:
        _browser_login_semaphore.release()
        raise
    return True


@contextmanager
def _browser_login_slot(deadline=None):
    """Hold one cold-browser slot and always return it to the pool."""
    _acquire_browser_login(deadline)
    try:
        yield
    finally:
        _browser_login_semaphore.release()


@contextmanager
def _browser_playwright():
    """Load Playwright after a browser slot has been acquired."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        yield playwright


def recover_from_idm_error_page(page, username, timeout, recovery_url=None, deadline=None):
    """Re-open the login entry when the IDM shows its verification-failed page.

    Returns ``None`` when there was nothing to recover, ``True`` when the entry
    was re-opened, and the HTTP status when the re-open itself was rejected --
    the caller then keeps its retry budget instead of aborting immediately.
    """
    _remaining_seconds(deadline)
    try:
        body_text = page.locator("body").inner_text(timeout=min(2000, _browser_timeout_ms(timeout, deadline)))
    except Exception:
        body_text = ""
    if not is_device_verification_failure(body_text):
        return None

    logger.warning("账号 %s: 统一认证页面提示验证失败，尝试重新打开登录入口。", _debug_user_id(username))
    try:
        _remaining_seconds(deadline)
        if recovery_url:
            response = page.goto(
                recovery_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms(timeout, deadline)
            )
            status = _http_status(response)
            if status is not None and status >= 400:
                logger.warning(
                    "账号 %s: 重新打开登录入口返回 HTTP %s，稍后重试。",
                    _debug_user_id(username),
                    status,
                )
                return status
        else:
            link = page.locator(BACK_TO_LOGIN_LINK_SELECTOR).first
            href = link.get_attribute("href", timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            if href:
                logger.debug("账号 %s: 返回登录页面链接：%s", _debug_user_id(username), _redact_url(href)[:200])
                response = page.goto(
                    urllib.parse.urljoin(page.url, href),
                    wait_until="domcontentloaded",
                    timeout=_browser_timeout_ms(timeout, deadline),
                )
                status = _http_status(response)
                if status is not None and status >= 400:
                    logger.warning(
                        "账号 %s: 返回登录页面返回 HTTP %s，稍后重试。",
                        _debug_user_id(username),
                        status,
                    )
                    return status
            else:
                link.click(timeout=_browser_timeout_ms(timeout, deadline))
                page.wait_for_load_state("domcontentloaded", timeout=_browser_timeout_ms(timeout, deadline))
        logger.debug("账号 %s: 返回登录页面后 URL: %s", _debug_user_id(username), _redact_url(page.url))
        return True
    except Exception as exc:
        save_login_debug_artifacts(page, username, "idm_error_recovery_failed", exc)
        raise LoginError(
            FailureReason.LOGIN_PAGE_CHANGED,
            f"统一认证验证失败后无法返回登录页面: {_redact_text(exc)}",
        ) from None


def click_username_password_tab(page, username, timeout, deadline=None):
    _remaining_seconds(deadline)
    tab = page.locator(USERNAME_PASSWORD_TAB_SELECTOR).first
    try:
        if tab.count() > 0:
            logger.debug("账号 %s: 正在切换到用户名密码登录。", _debug_user_id(username))
            tab.click(timeout=_browser_timeout_ms(timeout, deadline))
            return True
    except Exception as exc:
        logger.debug("账号 %s: 切换用户名密码登录失败：%s", _debug_user_id(username), _redact_text(exc))
    return False


def login_name_locator(page):
    return page.locator(LOGIN_NAME_SELECTOR).first


def password_locator(page):
    return page.locator(PASSWORD_SELECTOR).first


def captcha_locator(page):
    return page.locator(CAPTCHA_IMAGE_SELECTOR).first


def route_login_resource(route):
    req = route.request
    res_type = req.resource_type
    url = req.url
    if res_type == "font":
        route.abort()
    elif res_type == "image":
        # 仅保留验证码图片和登录按钮图片，拦截其他非必要图片
        if is_captcha_resource(url):
            route.continue_()
        else:
            route.abort()
    else:
        route.continue_()


def get_captcha_image_bytes(page, captcha_el, timeout, deadline=None):
    _remaining_seconds(deadline)
    captcha_el.wait_for(state="visible", timeout=_browser_timeout_ms(timeout, deadline))
    # Read the image already rendered in this browser session. Fetching its
    # URL again can generate a new challenge and mutate the server session.
    try:
        page.wait_for_function(
            "img => img.complete && img.naturalWidth > 0",
            arg=captcha_el.element_handle(timeout=_browser_timeout_ms(timeout, deadline)),
            timeout=_browser_timeout_ms(timeout, deadline),
        )
    except Exception as exc:
        # A captcha that never renders is a captcha problem, not an unknown
        # browser failure: keep the exit code and the diagnostic precise.
        raise LoginError(FailureReason.CAPTCHA, f"验证码图片未加载完成: {_redact_text(exc)}") from None
    return captcha_el.screenshot(timeout=_browser_timeout_ms(timeout, deadline))


def captcha_input_locator(page):
    return page.locator(CAPTCHA_INPUT_SELECTOR).first


def submit_button_locator(page):
    return page.locator(SUBMIT_BUTTON_SELECTOR).first


def _login_form_ready(page, username, timeout, deadline=None):
    """Wait for the account/password controls and report whether they appeared."""
    try:
        login_name_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
        password_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
    except Exception:
        return False
    logger.debug("账号 %s: 已找到登录表单。", _debug_user_id(username))
    return True


def ensure_login_form(page, username, timeout, recovery_url=None, deadline=None):
    """Make sure the account/password form is on screen.

    HTTP error documents from the authentication chain are retried here instead
    of ending the login: the school's front end answers intermittently and
    ``get_token`` has its own retry budget.  Only after every attempt has
    failed is ``page_load`` raised, and only when an HTTP failure was actually
    seen; a missing form without one is still reported as a structure change.
    """
    last_http_status = None
    for attempt in range(1, 4):
        _remaining_seconds(deadline)
        logger.debug(
            "账号 %s: 正在确认登录表单 (第 %s/3 次)，当前 URL: %s",
            _debug_user_id(username),
            attempt,
            _redact_url(page.url),
        )
        last_http_status = _remember_http_status(
            recover_from_idm_error_page(page, username, timeout, recovery_url=recovery_url, deadline=deadline),
            last_http_status,
        )
        click_username_password_tab(page, username, timeout, deadline=deadline)
        if _login_form_ready(page, username, timeout, deadline):
            return

        button = page.locator(UNIFIED_LOGIN_BUTTON_SELECTOR).first
        try:
            if button.count() > 0:
                logger.debug("账号 %s: 正在点击统一认证登录按钮...", _debug_user_id(username))
                with page.expect_navigation(
                    wait_until="domcontentloaded",
                    timeout=_browser_timeout_ms(timeout, deadline),
                ) as navigation:
                    button.click(timeout=_browser_timeout_ms(timeout, deadline))
                navigation_status = _http_status(navigation.value)
                if navigation_status is not None and navigation_status >= 400:
                    logger.warning(
                        "账号 %s: 统一认证跳转返回 HTTP %s，重新尝试登录入口。",
                        _debug_user_id(username),
                        navigation_status,
                    )
                last_http_status = _remember_http_status(navigation_status, last_http_status)
                continue
        except Exception as exc:
            logger.debug(
                "账号 %s: 点击统一认证登录按钮失败 (第 %s/3 次): %s",
                _debug_user_id(username),
                attempt,
                _redact_text(exc),
            )

        last_http_status = _remember_http_status(
            recover_from_idm_error_page(page, username, timeout, recovery_url=recovery_url, deadline=deadline),
            last_http_status,
        )
        click_username_password_tab(page, username, timeout, deadline=deadline)
        if _login_form_ready(page, username, timeout, deadline):
            return

    save_login_debug_artifacts(page, username, "login_form_not_found")
    if last_http_status is not None:
        raise LoginError(FailureReason.PAGE_LOAD, f"认证页面返回 HTTP {last_http_status}")
    raise LoginError(FailureReason.LOGIN_PAGE_CHANGED, "未找到登录表单，登录页结构可能已变化")


def _browser_timeout_ms(timeout, deadline):
    """Convert the per-operation timeout to milliseconds without crossing deadline."""
    bounded = _timeout_with_deadline(timeout, deadline)
    if bounded is None:
        bounded = timeout
    try:
        return max(1, int(float(bounded) * 1000))
    except (TypeError, ValueError):
        return 1000


_PLATFORM_UA_TOKENS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
    "win32": "Windows NT 10.0; Win64; x64",
}
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _platform_user_agent_token(platform=None):
    """Return the User-Agent platform token for ``platform``."""
    key = sys.platform if platform is None else platform
    return _PLATFORM_UA_TOKENS.get(key, _PLATFORM_UA_TOKENS["win32"])


def _login_user_agent(browser):
    """Return a User-Agent the school WAF accepts.

    Headless Chromium advertises ``HeadlessChrome`` in its User-Agent.  The
    unified login host answers HTTP 400 for that token on the federation hop
    even when every other request header is identical, so a headless run has
    to present an ordinary Chrome User-Agent.  The version is taken from the
    running browser so the string does not go stale, and the platform token
    follows the host so the header agrees with the browser's own hints.

    ``SWU_LOGIN_UA`` overrides the whole string, and a browser version that
    cannot be parsed falls back to a fixed Chrome User-Agent rather than the
    headless default that the WAF rejects.
    """
    override = (os.getenv("SWU_LOGIN_UA") or "").strip()
    if override:
        return override
    version = str(getattr(browser, "version", "") or "").strip()
    match = re.match(r"(\d+)", version)
    if not match:
        logger.warning(
            "无法从浏览器版本 %s 推导 User-Agent，改用固定值。",
            _redact_text(version) or "<empty>",
        )
        return _FALLBACK_USER_AGENT
    return (
        f"Mozilla/5.0 ({_platform_user_agent_token()}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{match.group(1)}.0.0.0 Safari/537.36"
    )


def read_login_error_message(page, timeout, deadline=None):
    """Return the visible failure text the login page is showing, if any.

    The dialog can appear a moment after the response, so this gives it a short
    grace period instead of sampling once and reporting "no error".
    """
    wait_for_selector = getattr(page, "wait_for_selector", None)
    if wait_for_selector is not None:
        try:
            wait_for_selector(
                _LOGIN_ERROR_MESSAGE_SELECTOR,
                state="visible",
                timeout=min(2000, _browser_timeout_ms(timeout, deadline)),
            )
        except Exception:
            pass
    try:
        message = (page.evaluate(_LOGIN_ERROR_MESSAGE_JS) or "").strip()
    except Exception:
        message = ""
    if message:
        return message
    try:
        body_text = page.locator("body").inner_text(timeout=min(2000, _browser_timeout_ms(timeout, deadline)))
    except Exception:
        return ""
    if not isinstance(body_text, str):
        return ""
    for hint in _LOGIN_FAILURE_HINTS:
        index = body_text.find(hint)
        if index >= 0:
            return body_text[max(0, index - 40) : index + 60].strip()
    return ""
