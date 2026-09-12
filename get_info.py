import json
import requests
import urllib.parse
import re
import threading
import logging
import sys
import os
import hashlib
import time
from contextlib import contextmanager

from cache import _token_cache_lock, load_cached_token, save_cached_token
from school_api import (
    DeadlineExceeded,
    SwuBusinessError,
    SwuRequestError,
    TokenInvalidError,
    _api_json as _school_api_json,
    _remaining_seconds,
    _redact_text as _school_redact_text,
    _redact_url as _school_redact_url,
    _timeout_with_deadline,
    check_school_connectivity,
    create_school_session,
    get_dormitory,
    get_student_id,
    get_transition_today,
    request_with_retry,
)

class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                safe_msg = msg.replace("✅", "[OK]").replace("❌", "[FAIL]")
                encoding = getattr(stream, "encoding", "utf-8") or "utf-8"
                encoded_bytes = safe_msg.encode(encoding, errors="replace")
                decoded_msg = encoded_bytes.decode(encoding)
                stream.write(decoded_msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

logger = logging.getLogger("swu")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.abspath(os.getenv("SWU_CONFIG_DIR", BASE_DIR))


class LoginError(Exception):
    """A browser login failed for a reason understood by the CLI."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def setup_logging():
    log_level_str = os.getenv("SWU_LOG_LEVEL", "INFO").upper().strip()
    levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL
    }
    level = levels.get(log_level_str, logging.INFO)
    
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        
    root_logger.setLevel(level)
    
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    handler = SafeStreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

_ocr_init_lock = threading.Lock()
_ocr_classification_lock = threading.Lock()
_ocr_instance = None


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


_redact_url = _school_redact_url
_redact_text = _school_redact_text
_api_json = _school_api_json


def _debug_user_id(username):
    digest = hashlib.sha256(str(username).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"user-{digest}"


def save_login_debug_artifacts(page, username, reason, error=None):
    """Write a small, redacted diagnostic record.

    Login pages contain credentials in form controls and sometimes include
    one-time tickets in their URL.  We intentionally do not persist page HTML
    or screenshots.  The text record contains only URL metadata, selector
    presence, and redacted error information.
    """
    debug_dir = os.getenv("SWU_DEBUG_DIR", "").strip()
    if not debug_dir:
        return
    safe_user = _debug_user_id(username)
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reason))[:40] or "debug"
    prefix = os.path.join(debug_dir, f"login_{safe_user}_{safe_reason}_{int(time.time() * 1000)}")
    try:
        os.makedirs(debug_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(debug_dir, 0o700)
        except OSError:
            pass
        page_url = _redact_url(getattr(page, "url", ""))
        try:
            title = _redact_text(page.title(), [username])
        except Exception:
            title = ""
        selectors = (
            "input#loginName, input[name=IDToken1]",
            "input#password, input[name=IDToken2]",
            "input#validateCode, input[name=IDToken3]",
            "button:has-text(登录), input[type=submit]",
        )
        selector_state = {}
        for selector in selectors:
            try:
                selector_state[selector] = bool(page.locator(selector).count())
            except Exception:
                selector_state[selector] = None
        lines = [
            f"reason: {_redact_text(reason, [username])}",
            f"url: {page_url}",
            f"title: {title}",
            f"error_type: {type(error).__name__ if error is not None else ''}",
            f"error: {_redact_text(error, [username])[:1000] if error is not None else ''}",
            "selectors: " + json.dumps(selector_state, ensure_ascii=False, sort_keys=True),
        ]
        with open(f"{prefix}.txt", "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(f"{prefix}.txt", 0o600)
        except OSError:
            pass
        logger.info("账号 %s: 已保存登录诊断：%s.txt", safe_user, prefix)
    except Exception as exc:
        logger.warning("账号 %s: 保存登录诊断失败：%s", safe_user, _redact_text(exc, [username]))


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
    return "waf_blocked" if last_login_status == 400 else "captcha"


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
    if "动态口令验证失败" not in body_text and "验证失败" not in body_text:
        return None

    logger.warning("账号 %s: 统一认证页面提示验证失败，尝试重新打开登录入口。", _debug_user_id(username))
    try:
        _remaining_seconds(deadline)
        if recovery_url:
            response = page.goto(recovery_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms(timeout, deadline))
            status = _http_status(response)
            if status is not None and status >= 400:
                logger.warning(
                    "账号 %s: 重新打开登录入口返回 HTTP %s，稍后重试。",
                    _debug_user_id(username),
                    status,
                )
                return status
        else:
            link = page.locator('a:has-text("返回至登录页面")').first
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
        raise LoginError("login_page_changed", f"统一认证验证失败后无法返回登录页面: {exc}")


def click_username_password_tab(page, username, timeout, deadline=None):
    _remaining_seconds(deadline)
    tab = page.locator('text="用户名密码"').first
    try:
        if tab.count() > 0:
            logger.debug("账号 %s: 正在切换到用户名密码登录。", _debug_user_id(username))
            tab.click(timeout=_browser_timeout_ms(timeout, deadline))
            return True
    except Exception as exc:
        logger.debug("账号 %s: 切换用户名密码登录失败：%s", _debug_user_id(username), _redact_text(exc))
    return False


def login_name_locator(page):
    return page.locator('input#loginName, input[name="IDToken1"], input[placeholder*="用户名"], input[placeholder*="账号"]').first


def password_locator(page):
    return page.locator('input#password, input[name="IDToken2"], input[type="password"], input[placeholder*="密码"]').first


def captcha_locator(page):
    return page.locator('img#kaptchaImage, img[src*="kaptcha"], img[src*="captcha"]').first


def route_login_resource(route):
    req = route.request
    res_type = req.resource_type
    url = req.url
    if res_type == "font":
        route.abort()
    elif res_type == "image":
        # 仅保留验证码图片和登录按钮图片，拦截其他非必要图片
        if (
            "kaptchaImage" in url
            or "unified_button" in url
            or urllib.parse.urlsplit(url).path == "/am/validate.code"
        ):
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
        raise LoginError("captcha", f"验证码图片未加载完成: {_redact_text(exc)}")
    return captcha_el.screenshot(timeout=_browser_timeout_ms(timeout, deadline))


def captcha_input_locator(page):
    return page.locator('input#validateCode, input[name="IDToken3"], input[placeholder*="验证码"], input[placeholder*="校验码"]').first


def submit_button_locator(page):
    return page.locator('input#button, button:has-text("登录"), input[type="submit"], .loginBtn, .btn-login').first


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
            recover_from_idm_error_page(
                page, username, timeout, recovery_url=recovery_url, deadline=deadline
            ),
            last_http_status,
        )
        click_username_password_tab(page, username, timeout, deadline=deadline)
        if _login_form_ready(page, username, timeout, deadline):
            return

        button = page.locator('img[src*="unified_button"]').first
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
            recover_from_idm_error_page(
                page, username, timeout, recovery_url=recovery_url, deadline=deadline
            ),
            last_http_status,
        )
        click_username_password_tab(page, username, timeout, deadline=deadline)
        if _login_form_ready(page, username, timeout, deadline):
            return

    save_login_debug_artifacts(page, username, "login_form_not_found")
    if last_http_status is not None:
        raise LoginError("page_load", f"认证页面返回 HTTP {last_http_status}")
    raise LoginError("login_page_changed", "未找到登录表单，登录页结构可能已变化")


def get_ocr():
    """Return the process-wide lazily initialized OCR engine.

    The ddddocr object owns a relatively large model.  Initializing one per
    worker thread multiplies that cost, so construction is guarded and the
    resulting object is shared.  Classification itself is guarded separately
    because the library does not promise that one object is thread-safe.
    """
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_init_lock:
            if _ocr_instance is None:
                import ddddocr

                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


def classify_captcha(image_bytes):
    """Classify captcha bytes through the shared OCR engine safely."""
    with _ocr_classification_lock:
        return get_ocr().classification(image_bytes)

def _browser_timeout_ms(timeout, deadline):
    """Convert the per-operation timeout to milliseconds without crossing deadline."""
    bounded = _timeout_with_deadline(timeout, deadline)
    if bounded is None:
        bounded = timeout
    try:
        return max(1, int(float(bounded) * 1000))
    except (TypeError, ValueError):
        return 1000


# ``navigator.platform`` and ``sec-ch-ua-platform`` still report the real host
# OS, so a hard-coded Windows token on a Linux runner is exactly the kind of
# contradiction a device check looks for.  ``SWU_LOGIN_UA`` pins an explicit
# string for a deployment that needs the historically validated value.
_PLATFORM_UA_TOKENS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
    "win32": "Windows NT 10.0; Win64; x64",
}
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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


# The CAS hop hands the browser a pair of opaque base62 device cookies that
# the browser then replays to the IDM host.
_OPAQUE_COOKIE_NAME = re.compile(r"^[0-9A-Za-z]{13}$")


def _cookie_reaches_login_host(cookie_domain, login_host):
    """Whether a cookie scoped to ``cookie_domain`` is sent to ``login_host``.

    Host-only cookies match exactly; a domain cookie matches the domain itself
    and every subdomain, which is why a device cookie issued for ``swu.edu.cn``
    would also be replayed to the login host.
    """
    domain = (cookie_domain or "").lstrip(".").rstrip(".").lower()
    if not domain:
        return False
    return login_host == domain or login_host.endswith("." + domain)


def _drop_federation_cookies(context, login_host, username):
    """Remove the CAS hop's opaque device cookies before submitting the form.

    ``uaaap.swu.edu.cn`` sets two opaque cookies that are replayed to
    ``idm.swu.edu.cn``.  While they are present, ``POST /am/UI/Login`` is
    answered with HTTP 400 and an empty body; after removing exactly those
    cookies the same POST reaches the authentication service and returns its
    normal result page.

    Deletion targets each cookie's exact ``name``/``domain`` pair, so the rest
    of the jar -- including the same cookie names on the CAS host -- stays
    intact.  Returns ``True`` when the jar is in the intended state.
    """
    host = (login_host or "").rstrip(".").lower()
    if not host:
        return False
    try:
        cookies = context.cookies()
    except Exception as exc:
        logger.warning("账号 %s: 读取浏览器 Cookie 失败：%s", _debug_user_id(username), _redact_text(exc))
        return False

    targets = sorted(
        {
            (cookie.get("domain") or "", cookie.get("name") or "")
            for cookie in cookies
            if _cookie_reaches_login_host(cookie.get("domain"), host)
            and _OPAQUE_COOKIE_NAME.match(cookie.get("name") or "")
        }
    )
    if not targets:
        return True

    for domain, name in targets:
        try:
            context.clear_cookies(name=name, domain=domain)
        except Exception as exc:
            # Everything else stays in the jar, so the caller can still retry
            # instead of continuing without any cookie at all.
            logger.warning(
                "账号 %s: 清除认证跳转遗留 Cookie 失败：%s",
                _debug_user_id(username),
                _redact_text(exc),
            )
            return False
    logger.debug(
        "账号 %s: 已清除认证跳转遗留 Cookie %s 个。",
        _debug_user_id(username),
        len(targets),
    )
    return True


def _absolute_redirect_target(response, location):
    """Resolve a login response's ``Location`` against its own URL.

    The login response points at the plain-HTTP form of the authorize URL,
    whose redirect loses the authenticated session and bounces back to the
    login form, so replaying the same URL over HTTPS reaches the portal.  A
    relative ``Location`` is resolved first, which keeps that upgrade -- and
    the recovery path that uses this value -- applicable to it as well.
    """
    if not location:
        return None
    base = getattr(response, "url", "") or ""
    target = urllib.parse.urljoin(base, location) if base else location
    if target.startswith("http://"):
        target = "https://" + target[len("http://"):]
    return target


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


_load_cached_token = load_cached_token
_save_cached_token = save_cached_token

def _find_query_value_from_url(url, key):
    parsed = urllib.parse.urlparse(url)
    for part in [parsed.query, parsed.fragment]:
        values = urllib.parse.parse_qs(part).get(key)
        if values:
            return urllib.parse.unquote(values[0])
        if "?" in part:
            values = urllib.parse.parse_qs(part.split("?", 1)[1]).get(key)
            if values:
                return urllib.parse.unquote(values[0])
    if f"{key}=" in url:
        return urllib.parse.unquote(url.split(f"{key}=", 1)[1].split("&", 1)[0])
    return None


def _url_hostname(url):
    """Return a normalized URL hostname without inspecting query strings."""
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").rstrip(".").lower()
    except (TypeError, ValueError):
        return ""


def _is_school_host(url):
    """Whether ``url`` is hosted by the school's portal domain."""
    hostname = _url_hostname(url)
    return hostname == "of.swu.edu.cn" or hostname.endswith(".of.swu.edu.cn")


def _token_from_local_storage(value):
    """Extract an access token from the portal's stable local-storage shapes."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None

    # Prefer the canonical key in the complete tree before considering loose
    # legacy names.  Iteration order must not accidentally select a refresh or
    # identity token that happens to precede access_token.
    for key, item in value.items():
        if str(key).lower() in {"access_token", "accesstoken"} and isinstance(item, str) and item:
            return item
    for key, item in value.items():
        if isinstance(item, dict) or (isinstance(item, str) and "vuex" in str(key).lower()):
            token = _token_from_local_storage(item)
            if token:
                return token

    for key, item in value.items():
        key_text = str(key).lower()
        if key_text in {"refresh_token", "refreshtoken", "id_token", "idtoken"}:
            continue
        if isinstance(item, str) and item and ("token" in key_text or "auth" in key_text):
            return item
    return None


def _local_storage_token(page):
    """Read a token for explicit login-success detection, if one exists."""
    try:
        local_storage = page.evaluate("() => JSON.stringify(localStorage)")
    except Exception:
        return None
    return _token_from_local_storage(local_storage)


def _login_success_detected(page, login_entry_url):
    """Detect a completed portal login from host and one-time/token state.

    Looking for a domain substring in the complete URL is unsafe because the
    initial CAS URL embeds the portal domain in its ``service`` query value.
    Parse the actual hostname and require a ticket or token instead.
    """
    current_url = getattr(page, "url", "")
    if not _is_school_host(current_url):
        return False
    ticket = _find_query_value_from_url(current_url, "ticket")
    token = _local_storage_token(page)
    if not ticket and not token:
        return False
    return current_url != login_entry_url or bool(token)


def _wait_for_login_result(page, login_entry_url, timeout, deadline=None):
    """Wait for a ticket/token condition without waiting for network quiescence."""
    _remaining_seconds(deadline)
    wait_for_function = getattr(page, "wait_for_function", None)
    if wait_for_function is not None:
        expression = """({ entryUrl }) => {
            const popup = document.querySelector('.pop .ctnTxt');
            if (popup && popup.getClientRects().length && popup.innerText.trim()) {
                return true;
            }
            const parsed = new URL(window.location.href);
            const host = (parsed.hostname || '').toLowerCase().replace(/\\.$/, '');
            if (host !== 'of.swu.edu.cn' && !host.endsWith('.of.swu.edu.cn')) {
                return false;
            }
            const hasTicket = parsed.searchParams.has('ticket') ||
                new URLSearchParams(parsed.hash.replace(/^#/, '')).has('ticket');
            const hasToken = Object.keys(window.localStorage).some((key) => {
                const lower = key.toLowerCase();
                return lower === 'access_token' || lower.includes('token') || lower.includes('auth');
            });
            return hasTicket || hasToken;
        }"""
        try:
            wait_for_function(
                expression,
                arg={"entryUrl": login_entry_url},
                timeout=_browser_timeout_ms(timeout, deadline),
            )
        except DeadlineExceeded:
            raise
        except Exception:
            # A timeout means the captcha/error path should be inspected below.
            # Other browser exceptions are handled by the existing login error
            # mapping after the final explicit condition check.
            pass
    _remaining_seconds(deadline)
    return _login_success_detected(page, login_entry_url)


# ``closelert()`` on the IDM page only fades the dialog out; the node stays in
# the DOM with its text, so a dismissed message must not be read again on the
# next attempt.  Only visible nodes are collected here.
_LOGIN_ERROR_MESSAGE_JS = """() => {
    const seen = new Set();
    const nodes = document.querySelectorAll(
        '.pop .ctnTxt, .error, #error, .errorMessage, #errorMessage, .messager-body'
    );
    for (const node of nodes) {
        if (!node.getClientRects().length) {
            continue;
        }
        const text = (node.innerText || '').trim();
        if (text) {
            seen.add(text);
        }
    }
    return Array.from(seen).join('\\n');
}"""

_LOGIN_ERROR_MESSAGE_SELECTOR = (
    ".pop .ctnTxt, .error, #error, .errorMessage, #errorMessage, .messager-body"
)

# Some rejections are rendered as ordinary page text instead of the dialog
# node, so a bounded excerpt around a failure-specific phrase is used as a
# fallback.  The hints stay narrow on purpose: static labels on the form, such
# as the "用户名密码" tab, must not look like an error message.
_LOGIN_FAILURE_HINTS = (
    "验证失败",
    "动态口令验证失败",
    "用户名或密码",
    "密码错误",
    "密码不正确",
    "账号已被",
    "锁定",
    "不正确",
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
        body_text = page.locator("body").inner_text(
            timeout=min(2000, _browser_timeout_ms(timeout, deadline))
        )
    except Exception:
        return ""
    if not isinstance(body_text, str):
        return ""
    for hint in _LOGIN_FAILURE_HINTS:
        index = body_text.find(hint)
        if index >= 0:
            return body_text[max(0, index - 40) : index + 60].strip()
    return ""


def _validate_and_cache_token(username, token, cache_path, timeout, session, deadline):
    """Cache only a token that identifies the logged-in school account."""
    if not isinstance(token, str) or not token:
        raise LoginError("token_extract", "登录成功后提取到的 Token 格式无效")
    # The exchange endpoint returning HTTP 200 is not enough to prove that a
    # candidate local-storage value is the portal access token.  Validate its
    # identity through the existing user API before persisting it.
    get_student_id(
        token,
        timeout=min(timeout, 5),
        session=session,
        deadline=deadline,
    )
    _save_cached_token(username, token, cache_path)
    return token


def exchange_token_from_browser_page(page, ticket, timeout, deadline=None):
    if not ticket:
        return None
    _remaining_seconds(deadline)
    timeout_ms = _browser_timeout_ms(timeout, deadline)
    try:
        result = page.evaluate(
            """async ({ ticket, timeoutMs }) => {
            const url = `/gateway/fighter-middle/api/integrate/uaap/cas/exchange-token?token=${encodeURIComponent(ticket)}&remember=true`;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch(url, { credentials: 'include', signal: controller.signal });
                const text = await response.text();
                let data = null;
                try {
                    data = JSON.parse(text);
                } catch (error) {
                    data = null;
                }
                return { ok: response.ok, status: response.status, data, text: text.slice(0, 500) };
            } finally {
                clearTimeout(timer);
            }
            }""",
            {"ticket": ticket, "timeoutMs": timeout_ms},
        )
    except Exception as exc:
        _remaining_seconds(deadline)
        logger.debug("浏览器 exchange-token 请求异常：%s", _redact_text(exc))
        return None
    _remaining_seconds(deadline)
    if not isinstance(result, dict):
        logger.debug("浏览器 exchange-token 返回结构异常")
        return None
    if not result.get("ok"):
        logger.debug(
            "浏览器 exchange-token 请求未成功：HTTP %s %s",
            result.get("status"),
            _redact_text(result.get("text")),
        )
        return None
    data = result.get("data")
    if isinstance(data, str) and data:
        return data
    if isinstance(data, dict):
        token = data.get("data") or data.get("token") or data.get("access_token")
        if isinstance(token, str) and token:
            return token
    token = result.get("token") or result.get("access_token")
    if isinstance(token, str) and token:
        return token
    logger.debug(
        "浏览器 exchange-token 未返回 Token：HTTP %s %s",
        result.get("status"),
        _redact_text(result.get("text")),
    )
    return None


# Login entries.  ``ywtb`` starts from the school's 一网通办 portal the way a
# user does and then lets the portal entry finish from that CAS session;
# ``portal`` keeps the previous single-entry behaviour.
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
                raise LoginError("page_load", f"认证页面返回 HTTP {exc.status}") from exc
            logger.warning(
                "账号 %s: 登录页返回 HTTP %s (第 %s 次尝试)，正在重新载入...",
                _debug_user_id(username),
                exc.status,
                attempt,
            )
        except Exception as exc:
            _remaining_seconds(deadline)
            if attempt == 2:
                raise LoginError("page_load", f"登录页加载失败或超时: {_redact_text(exc)}")
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
                    "page_load",
                    f"填写登录表单失败且认证页面返回 HTTP {recovery_status}: "
                    f"{_redact_text(exc, [username, password])}",
                )
            raise LoginError(
                "login_page_changed",
                f"填写登录表单失败: {_redact_text(exc, [username, password])}",
            )

        # Capture captcha image bytes
        captcha_el = captcha_locator(page)
        img_bytes = get_captcha_image_bytes(page, captcha_el, timeout, deadline=deadline)

        # Solve captcha
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
        logger.debug("账号 %s: 提交表单中...", _debug_user_id(username))
        redirect_target = None
        login_status = None
        try:
            with page.expect_response(
                lambda response: getattr(response.request, "method", "") == "POST"
                and "/am/UI/Login" in response.url,
                timeout=min(5000, _browser_timeout_ms(timeout, deadline)),
            ) as login_response:
                submit_button_locator(page).click(
                    timeout=_browser_timeout_ms(timeout, deadline)
                )
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
            if any(k in error_msg for k in ["密码", "账户", "用户名", "动态口令", "不正确"]):
                if "验证码" not in error_msg:
                    raise LoginError("credential", f"账号或密码错误: {safe_error_msg}")

        if "验证码" in error_msg:
            confirm = page.locator(".pop .confirm").first
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
    cache_path = os.path.join(CONFIG_DIR, ".token_cache.json")

    _remaining_seconds(deadline)
    # Try cached token first (unless force_login is True)
    if not force_login:
        cached_token = _load_cached_token(username, cache_path)
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
            _load_login_entry(page, username, entry_url, timeout, deadline)
            login_entry_url = page.url

            def wait_for_portal_result(redirect_target, login_status):
                """The portal entry has to reach the portal Token."""
                redirected = _wait_for_login_result(
                    page,
                    login_entry_url,
                    timeout if redirect_target else min(5, timeout),
                    deadline=deadline,
                )
                hop_accepted = redirect_target is not None or (
                    login_status is not None and 300 <= login_status < 400
                )
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
                logger.debug(
                    "账号 %s: 正在通过门户入口获取 Token...", _debug_user_id(username)
                )
                _load_login_entry(page, username, cas_url, timeout, deadline)
                if not _wait_for_login_result(
                    page, login_entry_url, timeout, deadline=deadline
                ):
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
                raise LoginError("token_extract", "登录成功后无法从 localStorage 中提取 Token")

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
            if "动态口令验证失败" in body_text or "验证失败" in body_text:
                raise LoginError("login_page_changed", f"统一认证错误页未能恢复到登录表单: {_redact_text(e, [username, password])}")
            raise LoginError("unknown", f"获取令牌失败: {_redact_text(e, [username, password])}")
        finally:
            browser.close()
