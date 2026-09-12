"""浏览器登录状态机。

登录路径显式分成下面几个阶段，每个阶段进入时都会打一条 DEBUG 级别的阶段标记，
方便学校修改认证流程后快速定位失败点：

``ENTRY`` → ``CENTRAL_AUTH`` → ``IDM_FORM`` → ``CAPTCHA`` → ``SUBMIT``
→ ``CAS_SESSION`` → ``PORTAL_AUTH`` → ``TOKEN``

一网通办入口（默认）先在该入口完成登录，再用同一 CAS 会话通过门户入口换取
Token；``SWU_LOGIN_ENTRY=portal`` 可以回退到直接从门户入口登录。
"""

from __future__ import annotations

import logging
import os
import time
from enum import StrEnum

import requests

from .. import config
from ..api.school import (
    DeadlineExceeded,
    SwuBusinessError,
    SwuRequestError,
    TokenInvalidError,
    _redact_text,
    _redact_url,
    _remaining_seconds,
    get_student_id,
)
from ..cache import load_cached_token
from .browser import (
    _browser_login_slot,
    _browser_playwright,
    _browser_timeout_ms,
    _login_user_agent,
    captcha_input_locator,
    captcha_locator,
    ensure_login_form,
    get_captcha_image_bytes,
    login_name_locator,
    password_locator,
    read_login_error_message,
    recover_from_idm_error_page,
    route_login_resource,
    submit_button_locator,
)
from .captcha import classify_captcha
from .cookies import _drop_federation_cookies
from .debug import _debug_user_id, save_login_debug_artifacts
from .errors import (
    FailureReason,
    LoginError,
    _http_status,
    _login_failure_reason,
    _require_ok_http_status,
    _RetryableHttpError,
)
from .pages import (
    CONFIRM_BUTTON_SELECTOR,
    is_captcha_error,
    is_credential_error,
    is_device_verification_failure,
)
from .tokens import (
    _local_storage_token,
    _validate_and_cache_token,
    _wait_for_login_result,
    exchange_token_from_browser_page,
)
from .urls import _absolute_redirect_target, _find_query_value_from_url, _url_hostname

logger = logging.getLogger("swu")


class LoginStage(StrEnum):
    """登录链的阶段标记，只用于日志和排查。"""

    ENTRY = "entry"
    CENTRAL_AUTH = "central_auth"
    IDM_FORM = "idm_form"
    CAPTCHA = "captcha"
    SUBMIT = "submit"
    CAS_SESSION = "cas_session"
    PORTAL_AUTH = "portal_auth"
    TOKEN = "token"


def log_stage(username, stage: LoginStage) -> None:
    """记录一次阶段切换，不改变控制流。"""

    logger.debug("账号 %s: 登录阶段 -> %s", _debug_user_id(username), stage.value)


def _complete_oauth_hop(page, context, username, target, timeout, deadline=None):
    """Clear the re-issued device cookies and drive the post-login hop."""
    logger.debug(
        "账号 %s: 正在完成登录跳转（%s）。",
        _debug_user_id(username),
        _url_hostname(target) or "<unknown>",
    )
    _drop_federation_cookies(context, _url_hostname(target), username)
    try:
        response = page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=_browser_timeout_ms(timeout, deadline),
        )
    except Exception as exc:
        logger.debug("账号 %s: 登录跳转失败：%s", _debug_user_id(username), _redact_text(exc))
        return False
    logger.debug(
        "账号 %s: 登录跳转返回 HTTP %s，当前 URL: %s",
        _debug_user_id(username),
        getattr(response, "status", None),
        _redact_url(page.url),
    )
    return True


def _recover_blocked_oauth_hop(
    page,
    context,
    username,
    login_entry_url,
    target,
    timeout,
    deadline=None,
):
    """Re-drive the post-login OAuth hop after a device-cookie rejection.

    The successful login response re-issues the school's device cookies, and
    the hop that follows is answered with HTTP 400 while they are present.
    Chromium follows that redirect internally, so the rejected hop cannot be
    intercepted; dropping the cookies and requesting the target again hands the
    browser straight to the portal.  ``target`` may be ``None`` when the login
    response carried no usable ``Location``: the address the page already sits
    on is re-driven then, so this fallback stays reachable in that case too.
    """
    host = _url_hostname(page.url)
    logger.debug(
        "账号 %s: 登录跳转停留在 %s，尝试清除设备 Cookie 后重试。",
        _debug_user_id(username),
        host or "<unknown>",
    )
    if not _complete_oauth_hop(page, context, username, target or page.url, timeout, deadline):
        return False
    return _wait_for_login_result(page, login_entry_url, timeout, deadline=deadline)


_YWTB_ENTRY_URL = "https://ywtb.swu.edu.cn/"
_DEFAULT_LOGIN_ENTRY = "ywtb"

# Credentials are always entered on these hosts; leaving them means the entry
# has finished authenticating.
_CREDENTIAL_HOSTS = ("idm.swu.edu.cn", "uaaap.swu.edu.cn")

_LEFT_CREDENTIAL_HOSTS_JS = """() => {
    const host = (window.location.hostname || '').toLowerCase();
    if (!host) {
        return false;
    }
    return !host.endsWith('idm.swu.edu.cn') && !host.endsWith('uaaap.swu.edu.cn');
}"""


def _login_entry_choice():
    """Return the configured login entry, defaulting to the 一网通办 portal."""
    choice = (os.getenv("SWU_LOGIN_ENTRY") or "").strip().lower()
    return choice if choice in {"ywtb", "portal"} else _DEFAULT_LOGIN_ENTRY


def _login_entry_url(portal_url):
    """Return the URL the browser should start the login from."""
    if _login_entry_choice() == "portal":
        return portal_url
    return _YWTB_ENTRY_URL


def _login_left_credential_hosts(page):
    """Whether the browser is back on an entry site instead of IDM/CAS."""
    host = _url_hostname(getattr(page, "url", ""))
    if not host:
        return False
    return not any(host == item or host.endswith("." + item) for item in _CREDENTIAL_HOSTS)


def _wait_for_entry_login(page, timeout, deadline=None):
    """Wait until the credential pages are left after a successful sign-in."""
    _remaining_seconds(deadline)
    wait_for_function = getattr(page, "wait_for_function", None)
    if wait_for_function is not None:
        try:
            wait_for_function(
                _LEFT_CREDENTIAL_HOSTS_JS,
                timeout=_browser_timeout_ms(timeout, deadline),
            )
        except DeadlineExceeded:
            raise
        except Exception:
            # A timeout only means the explicit check below decides.
            pass
    _remaining_seconds(deadline)
    return _login_left_credential_hosts(page)


def _load_login_entry(page, username, entry_url, timeout, deadline=None):
    """Open an authentication entry, retrying once on a transient error."""
    for attempt in range(1, 3):
        _remaining_seconds(deadline)
        try:
            response = page.goto(
                entry_url,
                wait_until="domcontentloaded",
                timeout=_browser_timeout_ms(timeout, deadline),
            )
            _require_ok_http_status(_http_status(response))
            return
        except _RetryableHttpError as exc:
            _remaining_seconds(deadline)
            if attempt == 2:
                raise LoginError(FailureReason.PAGE_LOAD, f"认证页面返回 HTTP {exc.status}") from exc
            logger.warning(
                "账号 %s: 登录页返回 HTTP %s (第 %s 次尝试)，正在重新载入...",
                _debug_user_id(username),
                exc.status,
                attempt,
            )
        except Exception as exc:
            _remaining_seconds(deadline)
            if attempt == 2:
                raise LoginError(FailureReason.PAGE_LOAD, f"登录页加载失败或超时: {_redact_text(exc)}") from None
            logger.warning(
                "账号 %s: 页面加载失败 (第 %s 次尝试): %s。正在重新载入...",
                _debug_user_id(username),
                attempt,
                _redact_text(exc),
            )


def _submit_login_form(
    page,
    context,
    username,
    password,
    timeout,
    deadline,
    *,
    recovery_url,
    wait_for_result,
):
    """Fill the credential form and submit until ``wait_for_result`` agrees.

    ``wait_for_result(redirect_target, login_status)`` decides whether the
    submission completed: the 一网通办 entry only has to leave the credential
    pages, while the portal entry has to reach the portal Token.
    """
    last_login_status = None
    for attempt in range(3):
        _remaining_seconds(deadline)
        logger.debug(
            "账号 %s: 正在填写登录表单并识别验证码 (尝试 %s/3)...",
            _debug_user_id(username),
            attempt + 1,
        )
        ensure_login_form(page, username, timeout, recovery_url=recovery_url, deadline=deadline)
        log_stage(username, LoginStage.IDM_FORM)
        # Fill credentials
        try:
            form_timeout = min(5000, _browser_timeout_ms(timeout, deadline))
            login_name_locator(page).fill(username, timeout=form_timeout)
            password_locator(page).fill(password, timeout=form_timeout)
        except Exception as exc:
            recovery_status = recover_from_idm_error_page(
                page, username, timeout, recovery_url=recovery_url, deadline=deadline
            )
            save_login_debug_artifacts(page, username, "fill_login_form_failed", exc)
            if isinstance(recovery_status, int):
                raise LoginError(
                    FailureReason.PAGE_LOAD,
                    f"填写登录表单失败且认证页面返回 HTTP {recovery_status}: {_redact_text(exc, [username, password])}",
                ) from None
            raise LoginError(
                FailureReason.LOGIN_PAGE_CHANGED,
                f"填写登录表单失败: {_redact_text(exc, [username, password])}",
            ) from None

        # Capture captcha image bytes
        captcha_el = captcha_locator(page)
        img_bytes = get_captcha_image_bytes(page, captcha_el, timeout, deadline=deadline)

        # Solve captcha
        log_stage(username, LoginStage.CAPTCHA)
        code = classify_captcha(img_bytes)
        logger.debug("账号 %s: 已识别验证码", _debug_user_id(username))

        captcha_input = captcha_input_locator(page)
        # Fill the field in one step.  Any keystroke or clear would fire the IDM
        # page's ``keyup`` handler, which calls ``verifyCode()``; that AJAX probe
        # answers HTTP 400 today, so the handler treats every code as wrong and
        # flips the page's global ``state`` to false, after which
        # ``portalLogin()`` returns early and the form is never submitted at
        # all.  A single ``fill`` leaves ``state`` true so the server validates
        # the code together with the credentials.
        captcha_input.fill(code, timeout=_browser_timeout_ms(timeout, deadline))

        # The authentication service answers the login POST with HTTP 400 while
        # the CAS hop's device cookies are present, and the page keeps
        # re-creating them, so clear them right before the submit instead of
        # once when the form first appears.  The host has to be resolved here:
        # the entry URL may still point at the CAS hop, while the form lives on
        # the login host.
        login_host = _url_hostname(page.url)
        _drop_federation_cookies(context, login_host, username)

        # Click login
        log_stage(username, LoginStage.SUBMIT)
        logger.debug("账号 %s: 提交表单中...", _debug_user_id(username))
        redirect_target = None
        login_status = None
        try:
            with page.expect_response(
                lambda response: getattr(response.request, "method", "") == "POST" and "/am/UI/Login" in response.url,
                timeout=min(5000, _browser_timeout_ms(timeout, deadline)),
            ) as login_response:
                submit_button_locator(page).click(timeout=_browser_timeout_ms(timeout, deadline))
            login_status = _http_status(login_response.value)
            redirect_target = _absolute_redirect_target(
                login_response.value,
                (login_response.value.headers or {}).get("location"),
            )
            if login_status == 400:
                logger.warning(
                    "账号 %s: 登录 POST 返回 HTTP 400，符合统一认证前置拦截特征。",
                    _debug_user_id(username),
                )
            logger.debug(
                "账号 %s: 登录响应 HTTP %s，跳转目标 %s",
                _debug_user_id(username),
                login_status,
                "已获取" if redirect_target else "缺失",
            )
        except Exception as exc:
            # A missing response means the page refused to submit; the
            # captcha/error handling below reports that case.
            logger.debug(
                "账号 %s: 未观察到登录表单提交响应：%s",
                _debug_user_id(username),
                _redact_text(exc),
            )
        last_login_status = login_status

        if redirect_target:
            # The successful login response re-issues the device cookies the
            # authentication service rejects, and the redirect it triggers is
            # answered with HTTP 400 before bouncing back to the login form.
            # Clear them right away and request the target over HTTPS so the
            # authenticated session survives the hop.
            log_stage(username, LoginStage.CAS_SESSION)
            _complete_oauth_hop(page, context, username, redirect_target, timeout, deadline)

        if wait_for_result(redirect_target, login_status):
            logger.debug("账号 %s: 重定向成功！", _debug_user_id(username))
            return
        logger.debug(
            "账号 %s: 登录结果检查完成，当前 URL: %s",
            _debug_user_id(username),
            _redact_url(page.url),
        )

        # Check for visible error message.  The dialog can appear a moment after
        # the response, and a dismissed one stays in the DOM, so the reader
        # waits briefly and only reports nodes that are actually visible.
        error_msg = read_login_error_message(page, timeout, deadline=deadline).strip()

        if error_msg:
            safe_error_msg = _redact_text(error_msg, [username, password])
            logger.warning("账号 %s: 登录页面返回错误信息: %s", _debug_user_id(username), safe_error_msg)
            # A rejected password login surfaces as the same generic text, so
            # keep it out of the captcha bucket and let the user check the
            # credentials instead.
            if is_credential_error(error_msg):
                raise LoginError(FailureReason.CREDENTIAL, f"账号或密码错误: {safe_error_msg}") from None

        if is_captcha_error(error_msg):
            confirm = page.locator(CONFIRM_BUTTON_SELECTOR).first
            if confirm.count() and confirm.is_visible():
                confirm.click(timeout=_browser_timeout_ms(timeout, deadline))

        # If not redirected and no explicit credential error, refresh captcha
        # and try again.
        try:
            # Back off a little: the school rejects repeated submissions from
            # one client for a short while, and retrying instantly only deepens
            # that penalty.
            time.sleep(min(3, max(0.0, _remaining_seconds(deadline) or 3)))
            logger.debug(
                "账号 %s: 验证码识别错误或重定向未触发，刷新验证码重试...",
                _debug_user_id(username),
            )
            captcha_el.click(timeout=_browser_timeout_ms(timeout, deadline))
        except Exception:
            pass

    save_login_debug_artifacts(page, username, "captcha_or_redirect_failed")
    raise LoginError(
        _login_failure_reason(last_login_status),
        "验证码连续识别失败，或登录服务没有完成跳转",
    )


def get_token(
    username: str,
    password: str,
    timeout=15,
    session=None,
    force_login: bool = False,
    deadline=None,
):
    cache_path = os.path.join(config.get_config_dir(), ".token_cache.json")

    _remaining_seconds(deadline)
    # Try cached token first (unless force_login is True)
    if not force_login:
        cached_token = load_cached_token(username, cache_path)
        if cached_token:
            try:
                cached_timeout = min(timeout, 5)
                get_student_id(
                    cached_token,
                    timeout=cached_timeout,
                    session=session,
                    deadline=deadline,
                )
                logger.info("账号 %s: 使用缓存的有效 Token，跳过浏览器登录。", _debug_user_id(username))
                return cached_token
            except TokenInvalidError:
                logger.info("账号 %s: 缓存的 Token 已失效，正在通过浏览器重新登录...", _debug_user_id(username))
            except DeadlineExceeded:
                raise
            except (requests.exceptions.RequestException, SwuRequestError, SwuBusinessError) as exc:
                # A temporary network/API failure does not prove that the
                # cached token is invalid. Propagate it to the caller instead
                # of launching a browser and potentially hiding the outage.
                logger.warning(
                    "账号 %s: 暂时无法验证缓存 Token，停止本次运行并保留缓存：%s",
                    _debug_user_id(username),
                    _redact_text(exc),
                )
                raise
        else:
            logger.info("账号 %s: 未发现缓存的 Token，正在获取新 Token...", _debug_user_id(username))
    else:
        logger.info("账号 %s: 收到强制登录参数，跳过缓存，正在获取新 Token...", _debug_user_id(username))

    cas_url = (
        "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        "?service=https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle"
        "%2Fapi%2Fintegrate%2Fuaap%2Fcas%2Fresolve-cas-return"
        "%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
        "%252F%2523%252FcasLogin%253Ffrom%253D%25252FappCenter"
    )

    _remaining_seconds(deadline)
    logger.debug("账号 %s: 正在启动 Playwright Chromium 浏览器...", _debug_user_id(username))
    # The context manager imports Playwright only after a cold-login slot is
    # acquired, keeping the cached/API path entirely browser-free.
    with _browser_login_slot(deadline), _browser_playwright() as p:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--password-store=basic",
            "--no-proxy-server",
        ]
        browser = p.chromium.launch(
            headless=True,
            timeout=_browser_timeout_ms(timeout, deadline),
            args=args,
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_login_user_agent(browser),
        )
        page = context.new_page()
        page.route("**/*", route_login_resource)

        try:
            entry_url = _login_entry_url(cas_url)
            logger.debug(
                "账号 %s: 正在访问登录入口 %s ...",
                _debug_user_id(username),
                _url_hostname(entry_url),
            )
            log_stage(username, LoginStage.ENTRY)
            _load_login_entry(page, username, entry_url, timeout, deadline)
            login_entry_url = page.url
            log_stage(username, LoginStage.CENTRAL_AUTH)

            def wait_for_portal_result(redirect_target, login_status):
                """The portal entry has to reach the portal Token."""
                log_stage(username, LoginStage.PORTAL_AUTH)
                redirected = _wait_for_login_result(
                    page,
                    login_entry_url,
                    timeout if redirect_target else min(5, timeout),
                    deadline=deadline,
                )
                hop_accepted = redirect_target is not None or (login_status is not None and 300 <= login_status < 400)
                if not redirected and hop_accepted:
                    # The login response looked accepted, so re-drive the hop
                    # even when it carried no usable Location.
                    redirected = _recover_blocked_oauth_hop(
                        page,
                        context,
                        username,
                        login_entry_url,
                        redirect_target,
                        timeout,
                        deadline=deadline,
                    )
                return redirected

            def wait_for_entry_result(redirect_target, _login_status):
                """The 一网通办 entry only has to leave the credential pages."""
                return _wait_for_entry_login(
                    page,
                    timeout if redirect_target else min(10, timeout),
                    deadline=deadline,
                )

            if entry_url == cas_url:
                _submit_login_form(
                    page,
                    context,
                    username,
                    password,
                    timeout,
                    deadline,
                    recovery_url=cas_url,
                    wait_for_result=wait_for_portal_result,
                )
            else:
                # Sign in at the configured entry exactly like a user does; the
                # resulting CAS session is what the portal entry needs.
                _submit_login_form(
                    page,
                    context,
                    username,
                    password,
                    timeout,
                    deadline,
                    recovery_url=entry_url,
                    wait_for_result=wait_for_entry_result,
                )
                logger.debug("账号 %s: 正在通过门户入口获取 Token...", _debug_user_id(username))
                _load_login_entry(page, username, cas_url, timeout, deadline)
                if not _wait_for_login_result(page, login_entry_url, timeout, deadline=deadline):
                    # The CAS session did not carry over, or the portal asked
                    # for credentials again: sign in here as before.
                    _submit_login_form(
                        page,
                        context,
                        username,
                        password,
                        timeout,
                        deadline,
                        recovery_url=cas_url,
                        wait_for_result=wait_for_portal_result,
                    )

            # Extract token from localStorage.  The explicit ticket/token
            # condition above means no network-idle wait is needed here.
            log_stage(username, LoginStage.TOKEN)
            _remaining_seconds(deadline)
            logger.debug("账号 %s: 正在从 localStorage 提取 access_token...", _debug_user_id(username))

            ticket = _find_query_value_from_url(page.url, "ticket")
            if ticket:
                logger.debug("账号 %s: 已从跳转地址获取 CAS ticket，正在交换 Token...", _debug_user_id(username))
                token = exchange_token_from_browser_page(page, ticket, timeout, deadline=deadline)
                if token:
                    token = _validate_and_cache_token(
                        username,
                        token,
                        cache_path,
                        timeout,
                        session,
                        deadline,
                    )
                    logger.debug("账号 %s: 通过 CAS ticket 交换 Token 成功", _debug_user_id(username))
                    return token

            _remaining_seconds(deadline)
            token = _local_storage_token(page)

            if not token:
                save_login_debug_artifacts(page, username, "token_extract_failed")
                raise LoginError(FailureReason.TOKEN_EXTRACT, "登录成功后无法从 localStorage 中提取 Token")

            token = _validate_and_cache_token(
                username,
                token,
                cache_path,
                timeout,
                session,
                deadline,
            )
            logger.debug("账号 %s: Token 提取并缓存成功", _debug_user_id(username))
            return token

        except DeadlineExceeded:
            raise
        except TokenInvalidError:
            raise
        except (requests.exceptions.RequestException, SwuRequestError, SwuBusinessError):
            raise
        except LoginError as exc:
            save_login_debug_artifacts(page, username, getattr(exc, "reason", "login_error"), exc)
            raise
        except Exception as e:
            try:
                body_text = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body_text = ""
            save_login_debug_artifacts(page, username, "unexpected_browser_error", e)
            if is_device_verification_failure(body_text):
                raise LoginError(
                    FailureReason.LOGIN_PAGE_CHANGED,
                    f"统一认证错误页未能恢复到登录表单: {_redact_text(e, [username, password])}",
                ) from None
            raise LoginError(FailureReason.UNKNOWN, f"获取令牌失败: {_redact_text(e, [username, password])}") from None
        finally:
            browser.close()
