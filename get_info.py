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

def get_ocr():
    if not hasattr(_thread_local, "ocr"):
        import ddddocr
        _thread_local.ocr = ddddocr.DdddOcr(show_ad=False)
    return _thread_local.ocr

def request_with_retry(method, url, max_retries=3, backoff_factor=2, session=None, **kwargs):
    import time
    client = session or requests
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

def get_token(username: str, password: str, timeout=15, session=None, force_login: bool = False):
    import os
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token_cache.json")
    
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
            logger.info(f"账号 {username}: 未发现缓存的 Token，正在通过浏览器登录...")
    else:
        logger.info(f"账号 {username}: 收到强制登录参数，跳过缓存，正在通过浏览器登录...")

    cas_url = (
        "https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL"
        "?service=https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle"
        "%2Fapi%2Fintegrate%2Fuaap%2Fcas%2Fresolve-cas-return"
        "%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
        "%252F%2523%252FcasLogin%253Ffrom%253D%25252FappCenter"
    )

    logger.debug(f"账号 {username}: 正在启动 Playwright Chromium 浏览器...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--password-store=basic"
            ]
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
                        raise
                    logger.warning(f"账号 {username}: 页面加载失败 (第 {attempt} 次尝试): {e}。正在重新载入...")
                    page.wait_for_timeout(2000)


            # Click "统一认证登录"
            logger.debug(f"账号 {username}: 正在点击统一认证登录按钮...")
            page.locator('img[src*="unified_button"]').click()

            # Wait for loginName
            page.wait_for_selector('input#loginName', timeout=timeout * 1000)

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
                            raise Exception(f"登录失败: {error_msg}")

                # If not redirected and no explicit credential error, refresh captcha and try again
                try:
                    logger.debug(f"账号 {username}: 验证码识别错误或重定向未触发，刷新验证码重试...")
                    captcha_el.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            if not success:
                raise Exception("登录失败: 无法跳转到 of.swu.edu.cn (验证码多次识别失败或服务不可用)")

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
                raise Exception("无法从 localStorage 中提取登录 Token")

            _save_cached_token(username, token, cache_path)
            logger.debug(f"账号 {username}: Token 提取并缓存成功")
            return token

        except Exception as e:
            raise Exception(f"获取令牌失败：{str(e)}")
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