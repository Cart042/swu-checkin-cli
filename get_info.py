import json
import requests
import urllib.parse
import base64
import re
import threading
import logging
import sys
import os
from playwright.sync_api import sync_playwright
from des import des

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

_thread_local = threading.local()
_token_cache_lock = threading.Lock()
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}
STANDARD_PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

def _proxy_url_with_auth(proxy_url, username="", password=""):
    if not proxy_url or not username:
        return proxy_url
    parsed = urllib.parse.urlparse(proxy_url)
    if parsed.username:
        return proxy_url
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return proxy_url
    auth = urllib.parse.quote(username, safe="")
    if password:
        auth += f":{urllib.parse.quote(password, safe='')}"
    if parsed.netloc:
        return urllib.parse.urlunparse(parsed._replace(netloc=f"{auth}@{netloc}"))
    return f"{parsed.scheme}://{auth}@{netloc}"


def get_proxy_config():
    mode = os.getenv("SWU_PROXY_MODE", "auto").strip().lower()
    if mode in {"off", "false", "0", "none", "disable", "disabled"}:
        return None
    proxy_url = os.getenv("SWU_PROXY_URL", "").strip()
    source = "SWU_PROXY_URL"
    if not proxy_url and mode != "manual":
        for key in STANDARD_PROXY_ENV_KEYS:
            value = os.getenv(key, "").strip()
            if value:
                proxy_url = value
                source = key
                break
    if not proxy_url:
        return None
    if "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    username = os.getenv("SWU_PROXY_USERNAME", "").strip()
    password = os.getenv("SWU_PROXY_PASSWORD", "").strip()
    return {
        "server": proxy_url,
        "scheme": urllib.parse.urlparse(proxy_url).scheme.lower(),
        "username": username,
        "password": password,
        "requests_url": _proxy_url_with_auth(proxy_url, username, password),
        "source": source,
    }


def validate_proxy_config():
    proxy = get_proxy_config()
    if not proxy:
        return True, None
    parsed = urllib.parse.urlparse(proxy["server"])
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES:
        return False, f"不支持的代理协议：{parsed.scheme}，请使用 http、https、socks4 或 socks5"
    if not parsed.hostname:
        return False, "代理地址缺少主机名或 IP"
    return True, None


def mask_proxy_url(proxy_url):
    if not proxy_url:
        return ""
    parsed = urllib.parse.urlparse(proxy_url)
    if not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urllib.parse.urlunparse(parsed._replace(netloc=f"{host}{port}"))


def describe_proxy_config():
    proxy = get_proxy_config()
    if not proxy:
        return "未配置"
    auth_hint = "，已配置认证" if proxy.get("username") else ""
    return f"{mask_proxy_url(proxy['server'])}{auth_hint}（来源：{proxy.get('source', 'unknown')}）"


def apply_proxy_to_session(session):
    session.trust_env = False
    proxy = get_proxy_config()
    if proxy:
        session.proxies.update({
            "http": proxy["requests_url"],
            "https": proxy["requests_url"],
        })
    return session


def playwright_proxy_options():
    proxy = get_proxy_config()
    if not proxy:
        return None
    options = {"server": proxy["server"]}
    if proxy.get("username"):
        options["username"] = proxy["username"]
    if proxy.get("password"):
        options["password"] = proxy["password"]
    return options


def check_school_connectivity(timeout=5):
    session = apply_proxy_to_session(requests.Session())
    try:
        response = session.get("https://of.swu.edu.cn/", timeout=timeout)
        return True, f"HTTP {response.status_code}"
    except requests.exceptions.ProxyError as exc:
        return False, f"代理连接失败：{exc}"
    except requests.exceptions.SSLError as exc:
        return False, f"TLS/证书连接失败：{exc}"
    except requests.exceptions.Timeout as exc:
        return False, f"连接学校官网超时：{exc}"
    except requests.exceptions.ConnectionError as exc:
        return False, f"无法连接学校官网：{exc}"
    except requests.exceptions.RequestException as exc:
        return False, f"请求学校官网失败：{exc}"


def has_school_proxy_config():
    return get_proxy_config() is not None


def get_ocr():
    if not hasattr(_thread_local, "ocr"):
        import ddddocr
        _thread_local.ocr = ddddocr.DdddOcr(show_ad=False)
    return _thread_local.ocr

def request_with_retry(method, url, max_retries=3, backoff_factor=2, session=None, **kwargs):
    import time
    client = session or apply_proxy_to_session(requests.Session())
    for attempt in range(1, max_retries + 1):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code in [500, 502, 503, 504]:
                response.raise_for_status()
            return response
        except (requests.exceptions.RequestException, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == max_retries:
                logger.error(f"请求失败，已达最大重试次数 {max_retries}: {e}")
                raise
            sleep_time = backoff_factor ** attempt
            logger.warning(f"请求异常: {e}。将在 {sleep_time} 秒后进行第 {attempt + 1}/{max_retries} 次重试...")
            time.sleep(sleep_time)

def _load_cached_token(username, cache_path):
    import os
    with _token_cache_lock:
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                return cache.get(username)
            except Exception:
                pass
    return None

def _save_cached_token(username, token, cache_path):
    import os
    with _token_cache_lock:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass
        cache[username] = token
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def extract_login_params(response):
    parsed_url = urllib.parse.urlparse(response.url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    goto = query_params.get("goto", [""])[0]
    realm = query_params.get("realm", ["/"])[0]
    service = query_params.get("service", ["initService"])[0]
    
    state = None
    
    # 1. Try to find state in response.url
    url_unquoted = urllib.parse.unquote(urllib.parse.unquote(response.url))
    state_match = re.search(r'state=([a-f0-9]{32})', url_unquoted)
    if state_match:
        state = state_match.group(1)
        
    # 2. Try to find state inside decoded goto parameter
    if not state and goto:
        try:
            padding_needed = len(goto) % 4
            goto_padded = goto + "=" * (4 - padding_needed) if padding_needed else goto
            decoded_goto = base64.b64decode(goto_padded).decode('utf-8', errors='ignore')
            decoded_unquoted = urllib.parse.unquote(urllib.parse.unquote(decoded_goto))
            state_match = re.search(r'state=([a-f0-9]{32})', decoded_unquoted)
            if state_match:
                state = state_match.group(1)
        except Exception:
            pass
            
    # 3. Fallback: search history
    if not state:
        for hist in response.history:
            hist_unquoted = urllib.parse.unquote(urllib.parse.unquote(hist.url))
            state_match = re.search(r'state=([a-f0-9]{32})', hist_unquoted)
            if state_match:
                state = state_match.group(1)
                break
                
    # 4. If still not found, search inside history's decoded goto
    if not state:
        for hist in response.history:
            try:
                parsed_hist = urllib.parse.urlparse(hist.url)
                hist_params = urllib.parse.parse_qs(parsed_hist.query)
                hist_goto = hist_params.get("goto", [""])[0]
                if hist_goto:
                    padding_needed = len(hist_goto) % 4
                    goto_padded = hist_goto + "=" * (4 - padding_needed) if padding_needed else hist_goto
                    decoded_goto = base64.b64decode(goto_padded).decode('utf-8', errors='ignore')
                    decoded_unquoted = urllib.parse.unquote(urllib.parse.unquote(decoded_goto))
                    state_match = re.search(r'state=([a-f0-9]{32})', decoded_unquoted)
                    if state_match:
                        state = state_match.group(1)
                        break
            except Exception:
                pass
                
    return goto, realm, service, state


def _transform_ticket(ticket):
    ticket_parts = urllib.parse.unquote(ticket).split("-")
    if len(ticket_parts) < 3:
        raise LoginError("direct_login", "统一认证返回的 ticket 格式异常")

    str1 = ""
    str2 = ""
    for char in ticket_parts[1]:
        str1 += str((int(char) + 5) % 10)
    for char in ticket_parts[2]:
        if "0" <= char <= "9":
            str2 += str((int(char) + 5) % 10)
        elif "A" <= char <= "Z":
            str2 += chr(ord(char) + 10 - 26 if ord(char) + 10 > ord("Z") else ord(char) + 10)
        else:
            str2 += chr(ord(char) + 15 - 26 if ord(char) + 15 > ord("z") else ord(char) + 15)
    return str1, str2


def _find_query_value_from_response(response, key):
    candidates = [response]
    candidates.extend(getattr(response, "history", []) or [])
    for item in candidates:
        parsed = urllib.parse.urlparse(item.url)
        values = urllib.parse.parse_qs(parsed.query).get(key)
        if values:
            return values[0]
        if f"{key}=" in item.url:
            return item.url.split(f"{key}=", 1)[1].split("&", 1)[0]
    return None


def _login_response_hint(response):
    parsed = urllib.parse.urlparse(response.url)
    location = f"{parsed.netloc}{parsed.path}"
    history_count = len(getattr(response, "history", []) or [])
    text = getattr(response, "text", "") or ""
    error_hints = []
    for pattern in [
        r"(用户名或密码[^<\n\r]+)",
        r"(账号或密码[^<\n\r]+)",
        r"(密码错误[^<\n\r]*)",
        r"(验证码[^<\n\r]+)",
        r"(认证失败[^<\n\r]*)",
        r"(登录失败[^<\n\r]*)",
    ]:
        match = re.search(pattern, text)
        if match:
            error_hints.append(match.group(1).strip())
    suffix = f"，页面提示：{'; '.join(error_hints[:2])}" if error_hints else ""
    return f"HTTP {response.status_code}，最终地址 {location}，重定向 {history_count} 次{suffix}"


def get_token_direct(username: str, password: str, timeout=15, session=None):
    session = apply_proxy_to_session(session or requests.Session())
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    encrypted_username, encrypted_password = des(username, password)
    data = {
        "IDToken1": encrypted_username,
        "IDToken2": encrypted_password,
        "IDToken3": "",
        "goto": "aHR0cDovL2lkbS5zd3UuZWR1LmNuL2FtL29hdXRoMi9hdXRob3JpemU/c2VydmljZT1pbml0U2VydmljZSZyZXNwb25zZV90eXBlPWNvZGUmY2xpZW50X2lkPTdjMXpva29samw5YmJpaG82eXVvJnNjb3BlPXVpZCtjbit1c2VySWRDb2RlJnJlZGlyZWN0X3VyaT1odHRwcyUzQSUyRiUyRnVhYWFwLnN3dS5lZHUuY24lMkZjYXMlMkZsb2dpbiUzRnNlcnZpY2UlM0RodHRwcyUyNTNBJTI1MkYlMjUyRnVhYWFwLnN3dS5lZHUuY24lMjUyRmNhcyUyNTJGb2F1dGgyLjAlMjUyRmNhbGxiYWNrQXV0aG9yaXplJTI2b3JpZ2luYWxSZXF1ZXN0VXJsJTNEaHR0cHMlMjUzQSUyNTJGJTI1MkZ1YWFhcC5zd3UuZWR1LmNuJTI1MkZjYXMlMjUyRm9hdXRoMi4wJTI1MkZhdXRob3JpemUlMjUzRnJlc3BvbnNlX3R5cGUlMjUzRGNvZGUlMjUyNmNsaWVudF9pZCUyNTNEY2FzNiUyNTI2cmVkaXJlY3RfdXJpJTI1M0RodHRwcyUyNTI1M0ElMjUyNTJGJTI1MjUyRm9mLnN3dS5lZHUuY24lMjUyNTNBNDQzJTI1MjUyRmNhcyUyNTI1MkZvYXV0aCUyNTI1MkZjYWxsYmFjayUyNTI1MkZTV1VfQ0FTMl9GRURFUkFMJTI1MjZzdGF0ZSUyNTNEZTFlMTczODhlNzU4MjY3YjFiNzI2ZjM4Mjg0NDM5MWElMjUyNnNjb3BlJTI1M0RzaW1wbGUlMjZmZWRlcmFsRW5hYmxlJTNEdHJ1ZSZkZWNpc2lvbj1BbGxvdw==",
        "gotoOnFail": "",
        "sunQueryParamsString": "cmVhbG09LyZzZXJ2aWNlPWluaXRTZXJ2aWNlJg==",
        "encoded": "true",
        "gx_charset": "UTF-8",
    }
    cas_url = (
        "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        "?service=https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle"
        "%2Fapi%2Fintegrate%2Fuaap%2Fcas%2Fresolve-cas-return"
        "%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
        "%252F%2523%252FcasLogin%253Ffrom%253D%25252FappCenter"
    )

    try:
        response = request_with_retry("GET", cas_url, timeout=timeout, session=session)
        state_match = re.search(
            r"state=([a-f0-9]{32})",
            urllib.parse.unquote(urllib.parse.unquote(response.url)),
        )
        if not state_match:
            raise LoginError("direct_login", "CAS 跳转地址中没有找到 state 参数")
        state = state_match.group(1)

        response = request_with_retry(
            "POST",
            "https://idm.swu.edu.cn/am/UI/Login",
            data=data,
            allow_redirects=True,
            timeout=timeout,
            session=session,
        )
        ticket = _find_query_value_from_response(response, "ticket")
        if not ticket:
            raise LoginError("credential", f"统一认证未返回 ticket，可能是账号密码错误或接口策略变化（{_login_response_hint(response)}）")

        str1, str2 = _transform_ticket(ticket)
        code = f"CD-{str1}-{str2}-wiie://777.643.675.751:3537/rph"
        callback_url = urllib.parse.unquote(
            f"https://of.swu.edu.cn/cas/oauth/callback/SWU_CAS2_FEDERAL?code={code}@@hxbeat&state={state}"
        )
        response = request_with_retry("GET", callback_url, allow_redirects=True, timeout=timeout, session=session)
        st_ticket = _find_query_value_from_response(response, "ticket")
        if not st_ticket:
            raise LoginError("direct_login", f"CAS 回调后没有获取到 ST ticket（{_login_response_hint(response)}）")

        token_response = request_with_retry(
            "GET",
            f"https://of.swu.edu.cn/gateway/fighter-middle/api/integrate/uaap/cas/exchange-token?token={st_ticket}&remember=true",
            timeout=timeout,
            session=session,
        ).json()
        token = token_response.get("data")
        if not token:
            raise LoginError("token_extract", f"交换 Token 失败：{token_response}")
        return token
    except LoginError:
        raise
    except requests.exceptions.RequestException as exc:
        raise LoginError("page_load", f"纯 HTTP 登录链路请求失败: {exc}")
    except Exception as exc:
        raise LoginError("direct_login", f"纯 HTTP 登录链路失败: {exc}")


def get_token(username: str, password: str, timeout=15, session=None, force_login: bool = False):
    import os
    cache_path = os.path.join(CONFIG_DIR, ".token_cache.json")
    
    # Try cached token first (unless force_login is True)
    if not force_login:
        cached_token = _load_cached_token(username, cache_path)
        if cached_token:
            try:
                get_student_id(cached_token, timeout=min(timeout, 5), session=session)
                logger.info(f"账号 {username}: 使用缓存的有效 Token，跳过浏览器登录。")
                return cached_token
            except Exception:
                logger.info(f"账号 {username}: 缓存的 Token 已失效，正在通过浏览器重新登录...")
        else:
            logger.info(f"账号 {username}: 未发现缓存的 Token，正在获取新 Token...")
    else:
        logger.info(f"账号 {username}: 收到强制登录参数，跳过缓存，正在获取新 Token...")

    login_method = os.getenv("SWU_LOGIN_METHOD", "auto").strip().lower()
    if login_method not in {"auto", "direct", "browser"}:
        logger.warning(f"账号 {username}: SWU_LOGIN_METHOD={login_method} 无效，将使用 auto。")
        login_method = "auto"

    if login_method in {"auto", "direct"}:
        try:
            logger.info(f"账号 {username}: 正在尝试纯 HTTP 登录链路...")
            token = get_token_direct(username, password, timeout=timeout, session=session)
            _save_cached_token(username, token, cache_path)
            logger.info(f"账号 {username}: 纯 HTTP 登录成功，Token 已缓存。")
            return token
        except LoginError as exc:
            if login_method == "direct":
                raise
            logger.warning(f"账号 {username}: 纯 HTTP 登录失败，将回退到浏览器登录：{exc}")

    cas_url = (
        "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        "?service=https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle"
        "%2Fapi%2Fintegrate%2Fuaap%2Fcas%2Fresolve-cas-return"
        "%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
        "%252F%2523%252FcasLogin%253Ffrom%253D%25252FappCenter"
    )

    logger.debug(f"账号 {username}: 正在启动 Playwright Chromium 浏览器...")
    with sync_playwright() as p:
        launch_options = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--password-store=basic"
            ]
        }
        proxy_options = playwright_proxy_options()
        if proxy_options:
            launch_options["proxy"] = proxy_options
            logger.info(f"账号 {username}: 浏览器登录将使用代理：{describe_proxy_config()}")
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
            logger.debug(f"账号 {username}: 正在访问 CAS 登录页面...")
            # Load page with up to 2 retry attempts
            for attempt in range(1, 3):
                try:
                    page.goto(cas_url, wait_until="networkidle", timeout=timeout * 1000)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise LoginError("page_load", f"登录页加载失败或超时: {e}")
                    logger.warning(f"账号 {username}: 页面加载失败 (第 {attempt} 次尝试): {e}。正在重新载入...")
                    page.wait_for_timeout(2000)


            # Click "统一认证登录"
            logger.debug(f"账号 {username}: 正在点击统一认证登录按钮...")
            try:
                page.locator('img[src*="unified_button"]').click(timeout=timeout * 1000)
            except Exception as e:
                raise LoginError("login_page_changed", f"未找到统一认证登录按钮，登录页结构可能已变化: {e}")

            # Wait for loginName
            try:
                page.wait_for_selector('input#loginName', timeout=timeout * 1000)
            except Exception as e:
                raise LoginError("login_page_changed", f"未找到登录表单，登录页结构可能已变化: {e}")

            success = False
            # Try up to 3 times to solve captcha and submit
            for attempt in range(3):
                logger.debug(f"账号 {username}: 正在填写登录表单并识别验证码 (尝试 {attempt + 1}/3)...")
                # Fill credentials
                page.locator('input#loginName').fill(username)
                page.locator('input#password').fill(password)

                # Capture captcha image bytes
                captcha_el = page.locator('img#kaptchaImage')
                img_bytes = captcha_el.screenshot()

                # Solve captcha
                ocr = get_ocr()
                code = ocr.classification(img_bytes)
                logger.debug(f"账号 {username}: 识别到验证码 = {code}")

                page.locator('input[type="text"]#validateCode').fill(code)

                # Click login
                logger.debug(f"账号 {username}: 提交表单中...")
                page.locator('input#button').click()

                # Wait to check result
                redirected = False
                for step_check in range(5):
                    page.wait_for_timeout(1000)
                    logger.debug(f"账号 {username}: 等待重定向 (第 {step_check + 1} 秒)，当前 URL: {page.url}")
                    if "of.swu.edu.cn" in page.url:
                        redirected = True
                        break

                if redirected:
                    logger.debug(f"账号 {username}: 重定向成功！")
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
                    logger.warning(f"账号 {username}: 登录页面返回错误信息: {error_msg}")
                    if any(k in error_msg for k in ["密码", "账户", "用户名", "密码错误", "不正确"]):
                        if "验证码" not in error_msg:
                            raise LoginError("credential", f"账号或密码错误: {error_msg}")

                # If not redirected and no explicit credential error, refresh captcha and try again
                try:
                    logger.debug(f"账号 {username}: 验证码识别错误或重定向未触发，刷新验证码重试...")
                    captcha_el.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            if not success:
                raise LoginError("captcha", "验证码连续识别失败，或登录服务没有完成跳转")

            # Extract token from localStorage
            logger.debug(f"账号 {username}: 正在从 localStorage 提取 access_token...")
            page.wait_for_timeout(2000)
            local_storage = page.evaluate("() => JSON.stringify(localStorage)")
            ls_dict = json.loads(local_storage)

            token = None
            for k, v in ls_dict.items():
                if k == 'access_token':
                    token = v
                    break
                if 'token' in k.lower() or 'auth' in k.lower():
                    token = v
                if 'vuex' in k.lower():
                    try:
                        vx = json.loads(v)
                        def search_dict(dct):
                            for vk, vv in dct.items():
                                if isinstance(vv, dict):
                                    t = search_dict(vv)
                                    if t: return t
                                elif isinstance(vv, str) and ('token' in vk.lower() or 'auth' in vk.lower()):
                                    if len(vv) > 5:
                                        return vv
                            return None
                        t = search_dict(vx)
                        if t:
                            token = t
                    except Exception:
                        pass

            if not token:
                raise LoginError("token_extract", "登录成功后无法从 localStorage 中提取 Token")

            _save_cached_token(username, token, cache_path)
            logger.debug(f"账号 {username}: Token 提取并缓存成功")
            return token

        except LoginError:
            raise
        except Exception as e:
            raise LoginError("unknown", f"获取令牌失败: {str(e)}")
        finally:
            browser.close()

def get_student_id(token, timeout=10, session=None):
    url = "https://of.swu.edu.cn/gateway/fighter-middle/api/auth/user?appType=fighter-portal"
    headers = {"fighter-auth-token": token}
    response = request_with_retry("GET", url, headers=headers, timeout=timeout, session=session)
    student_id = response.json()["data"]["subject"]["username"]
    return student_id

def get_dormitory(token, timeout=10, session=None):
    url = "https://of.swu.edu.cn/gateway/fighter-baida/api/cqlc/getDormitory"
    headers = {"fighter-auth-token": token, "Content-Type": "application/json;charset=UTF-8"}
    response = request_with_retry("POST", url, headers=headers, data=json.dumps({}), timeout=timeout, session=session)
    return response.json()

def get_transition_today(token, timeout=10, session=None):
    url = "https://of.swu.edu.cn//gateway/fighter-baida/api/cqtj/getTransitionByToday"
    headers = {"fighter-auth-token": token}
    data = {"pageNum": 1, "pageSize": 1}
    response = request_with_retry("POST", url, headers=headers, data=data, timeout=timeout, session=session)
    records = response.json()["data"]["records"]
    return records[0] if records else None
