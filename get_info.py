import json
import requests
import urllib.parse
import base64
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


def recover_from_idm_error_page(page, username, timeout, recovery_url=None, deadline=None):
    _remaining_seconds(deadline)
    try:
        body_text = page.locator("body").inner_text(timeout=min(2000, _browser_timeout_ms(timeout, deadline)))
    except Exception:
        body_text = ""
    if "动态口令验证失败" not in body_text and "验证失败" not in body_text:
        return False

    logger.warning("账号 %s: 统一认证页面提示验证失败，尝试重新打开登录入口。", _debug_user_id(username))
    try:
        _remaining_seconds(deadline)
        if recovery_url:
            page.goto(recovery_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms(timeout, deadline))
        else:
            link = page.locator('a:has-text("返回至登录页面")').first
            href = link.get_attribute("href", timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            if href:
                logger.debug("账号 %s: 返回登录页面链接：%s", _debug_user_id(username), _redact_url(href)[:200])
                page.goto(
                    urllib.parse.urljoin(page.url, href),
                    wait_until="domcontentloaded",
                    timeout=_browser_timeout_ms(timeout, deadline),
                )
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


def get_captcha_image_bytes(page, captcha_el, timeout, deadline=None):
    _remaining_seconds(deadline)
    captcha_el.wait_for(state="visible", timeout=_browser_timeout_ms(timeout, deadline))
    try:
        handle = captcha_el.element_handle(timeout=_browser_timeout_ms(timeout, deadline))
        if handle:
            src = handle.get_attribute("src") or ""
            if src.startswith("data:image"):
                encoded = src.split(",", 1)[1]
                return base64.b64decode(encoded)
            if src:
                response = page.request.get(
                    urllib.parse.urljoin(page.url, src),
                    timeout=_browser_timeout_ms(timeout, deadline),
                )
                if response.ok:
                    return response.body()
    except Exception as exc:
        logger.debug("验证码图片请求获取失败，回退元素截图：%s", _redact_text(exc))
    return captcha_el.screenshot(timeout=_browser_timeout_ms(timeout, deadline))


def captcha_input_locator(page):
    return page.locator('input#validateCode, input[name="IDToken3"], input[placeholder*="验证码"], input[placeholder*="校验码"]').first


def submit_button_locator(page):
    return page.locator('input#button, button:has-text("登录"), input[type="submit"], .loginBtn, .btn-login').first


def ensure_login_form(page, username, timeout, recovery_url=None, deadline=None):
    for attempt in range(1, 4):
        _remaining_seconds(deadline)
        logger.debug(
            "账号 %s: 正在确认登录表单 (第 %s/3 次)，当前 URL: %s",
            _debug_user_id(username),
            attempt,
            _redact_url(page.url),
        )
        recover_from_idm_error_page(page, username, timeout, recovery_url=recovery_url, deadline=deadline)
        click_username_password_tab(page, username, timeout, deadline=deadline)
        try:
            login_name_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            password_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            logger.debug("账号 %s: 已找到登录表单。", _debug_user_id(username))
            return
        except Exception:
            pass

        button = page.locator('img[src*="unified_button"]').first
        try:
            if button.count() > 0:
                logger.debug("账号 %s: 正在点击统一认证登录按钮...", _debug_user_id(username))
                button.click(timeout=_browser_timeout_ms(timeout, deadline))
                continue
        except Exception as exc:
            logger.debug(
                "账号 %s: 点击统一认证登录按钮失败 (第 %s/3 次): %s",
                _debug_user_id(username),
                attempt,
                _redact_text(exc),
            )

        recover_from_idm_error_page(page, username, timeout, recovery_url=recovery_url, deadline=deadline)
        click_username_password_tab(page, username, timeout, deadline=deadline)
        try:
            login_name_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            password_locator(page).wait_for(timeout=min(3000, _browser_timeout_ms(timeout, deadline)))
            logger.debug("账号 %s: 已找到登录表单。", _debug_user_id(username))
            return
        except Exception:
            pass

    save_login_debug_artifacts(page, username, "login_form_not_found")
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
        launch_options = {
            "headless": True,
            "timeout": _browser_timeout_ms(timeout, deadline),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--password-store=basic"
            ]
        }
        launch_options["args"].append("--no-proxy-server")
        browser = p.chromium.launch(
            **launch_options
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 细粒度静态资源拦截以优化页面加载速度
        def handle_route(route):
            req = route.request
            res_type = req.resource_type
            url = req.url
            if res_type == "font":
                route.abort()
            elif res_type == "image":
                # 仅保留验证码图片和登录按钮图片，拦截其他非必要图片
                if "kaptchaImage" in url or "unified_button" in url:
                    route.continue_()
                else:
                    route.abort()
            else:
                route.continue_()

        page.route("**/*", handle_route)

        try:
            logger.debug("账号 %s: 正在访问 CAS 登录页面...", _debug_user_id(username))
            # Load page with up to 2 retry attempts
            for attempt in range(1, 3):
                _remaining_seconds(deadline)
                try:
                    page.goto(
                        cas_url,
                        wait_until="domcontentloaded",
                        timeout=_browser_timeout_ms(timeout, deadline),
                    )
                    break
                except Exception as e:
                    _remaining_seconds(deadline)
                    if attempt == 2:
                        raise LoginError("page_load", f"登录页加载失败或超时: {_redact_text(e)}")
                    logger.warning(
                        "账号 %s: 页面加载失败 (第 %s 次尝试): %s。正在重新载入...",
                        _debug_user_id(username),
                        attempt,
                        _redact_text(e),
                    )

            login_entry_url = page.url
            ensure_login_form(page, username, timeout, recovery_url=cas_url, deadline=deadline)

            success = False
            # Try up to 3 times to solve captcha and submit
            for attempt in range(3):
                _remaining_seconds(deadline)
                logger.debug(
                    "账号 %s: 正在填写登录表单并识别验证码 (尝试 %s/3)...",
                    _debug_user_id(username),
                    attempt + 1,
                )
                ensure_login_form(page, username, timeout, recovery_url=cas_url, deadline=deadline)
                # Fill credentials
                try:
                    form_timeout = min(5000, _browser_timeout_ms(timeout, deadline))
                    login_name_locator(page).fill(username, timeout=form_timeout)
                    password_locator(page).fill(password, timeout=form_timeout)
                except Exception as exc:
                    recover_from_idm_error_page(page, username, timeout, recovery_url=cas_url, deadline=deadline)
                    save_login_debug_artifacts(page, username, "fill_login_form_failed", exc)
                    raise LoginError("login_page_changed", f"填写登录表单失败: {_redact_text(exc, [username, password])}")

                # Capture captcha image bytes
                captcha_el = captcha_locator(page)
                img_bytes = get_captcha_image_bytes(page, captcha_el, timeout, deadline=deadline)

                # Solve captcha
                code = classify_captcha(img_bytes)
                logger.debug("账号 %s: 已识别验证码", _debug_user_id(username))

                captcha_input_locator(page).fill(
                    code,
                    timeout=_browser_timeout_ms(timeout, deadline),
                )

                # Click login
                logger.debug("账号 %s: 提交表单中...", _debug_user_id(username))
                submit_button_locator(page).click(timeout=_browser_timeout_ms(timeout, deadline))

                # Wait for an explicit portal-host ticket/token condition.
                # Waiting for network quiescence is both slower and brittle on
                # pages that keep analytics or polling requests open.
                redirected = _wait_for_login_result(
                    page,
                    login_entry_url,
                    timeout,
                    deadline=deadline,
                )
                logger.debug(
                    "账号 %s: 登录结果检查完成，当前 URL: %s",
                    _debug_user_id(username),
                    _redact_url(page.url),
                )

                if redirected:
                    logger.debug("账号 %s: 重定向成功！", _debug_user_id(username))
                    success = True
                    break

                # Check for visible error message
                error_msg = ""
                try:
                    error_msg = page.evaluate("() => { const el = document.querySelector('.error, #error, .errorMessage, #errorMessage, .messager-body'); return el ? el.innerText : ''; }")
                except Exception:
                    pass

                if error_msg:
                    error_msg = error_msg.strip()
                    safe_error_msg = _redact_text(error_msg, [username, password])
                    logger.warning("账号 %s: 登录页面返回错误信息: %s", _debug_user_id(username), safe_error_msg)
                    if any(k in error_msg for k in ["密码", "账户", "用户名", "密码错误", "不正确"]):
                        if "验证码" not in error_msg:
                            raise LoginError("credential", f"账号或密码错误: {safe_error_msg}")

                # If not redirected and no explicit credential error, refresh captcha and try again
                try:
                    logger.debug("账号 %s: 验证码识别错误或重定向未触发，刷新验证码重试...", _debug_user_id(username))
                    captcha_el.click(timeout=_browser_timeout_ms(timeout, deadline))
                except Exception:
                    pass

            if not success:
                save_login_debug_artifacts(page, username, "captcha_or_redirect_failed")
                raise LoginError("captcha", "验证码连续识别失败，或登录服务没有完成跳转")

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
