import time
import os
import json
import logging
import sys
import atexit

try:
    from get_info import *
    GET_INFO_IMPORT_ERROR = None
except ImportError as exc:
    GET_INFO_IMPORT_ERROR = exc

    def setup_logging():
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def describe_proxy_config():
        proxy_url = os.getenv("SWU_PROXY_URL", "").strip()
        source = "SWU_PROXY_URL"
        if not proxy_url and os.getenv("SWU_PROXY_MODE", "auto").strip().lower() != "manual":
            for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
                value = os.getenv(key, "").strip()
                if value:
                    proxy_url = value
                    source = key
                    break
        if not proxy_url:
            return "未配置"
        visible = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        return f"{visible}（来源：{source}）"

    def validate_proxy_config():
        proxy_url = os.getenv("SWU_PROXY_URL", "").strip()
        if not proxy_url and os.getenv("SWU_PROXY_MODE", "auto").strip().lower() != "manual":
            for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
                value = os.getenv(key, "").strip()
                if value:
                    proxy_url = value
                    break
        if not proxy_url:
            return True, None
        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        scheme = proxy_url.split("://", 1)[0].lower()
        if scheme not in {"http", "https", "socks4", "socks5"}:
            return False, f"不支持的代理协议：{scheme}，请使用 http、https、socks4 或 socks5"
        return True, None

    def apply_proxy_to_session(session):
        return session

    def check_school_connectivity(timeout=5):
        return False, f"依赖未加载，无法检查学校官网连通性：{GET_INFO_IMPORT_ERROR}"

    def has_school_proxy_config():
        return describe_proxy_config() != "未配置"

try:
    import requests
    REQUESTS_IMPORT_ERROR = None
except ImportError as exc:
    requests = None
    REQUESTS_IMPORT_ERROR = exc

from des import des

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.abspath(os.getenv("SWU_CONFIG_DIR", BASE_DIR))

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(CONFIG_DIR, ".env"))
except ImportError:
    pass

logger = logging.getLogger("swu.check_in")

STATUS_MESSAGES = {
    0: "今日暂无签到任务。",
    1: "签到成功。",
    2: "今日已签到，无需重复操作。",
    3: "账号或密码验证失败，请检查后重试。",
    4: "连接错误或请求超时，请稍后重试。",
    5: "请假中，请检查是否有打卡任务。",
    6: "登录页加载失败或超时，可能是学校服务或网络异常。",
    7: "验证码连续识别失败，请稍后重试或使用 --force-login。",
    8: "登录成功但 Token 提取失败，可能是页面结构变化。",
    9: "学校登录页结构可能变化，请更新脚本选择器。",
    10: "学校接口返回异常，可能是服务暂时不可用。",
    11: "Token 校验失败或已失效，请尝试 --force-login。",
}

LOGIN_REASON_STATUS = {
    "credential": 3,
    "page_load": 6,
    "captcha": 7,
    "token_extract": 8,
    "login_page_changed": 9,
}

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def check_in(username: str, password: str, timeout: int = 10, force_login: bool = False):
    session = requests.Session()
    apply_proxy_to_session(session)

    def vacation_enable(token, timeout, session):
        headers = {
            "fighter-auth-token": token
        }
        url = 'https://of.swu.edu.cn/gateway/fighter-baida/api/flow-ext/start-process-instance-by-key'
        params = {'processDefinitionKey': 'XSQJXJ'}
        response = request_with_retry("POST", url, headers=headers, params=params, json={}, timeout=timeout, session=session)
        code = response.json()["code"]
        if code == 200 or code == 1100:
            return 0
        else:
            return 1

    def checkin_post(token, timeout, session, transition_today):
        try:
            if transition_today is None:
                return None
            formid = transition_today["formId"]
            id = transition_today["id"]
            headers = {"fighter-auth-token": token, "Content-Type": "application/json;charset=UTF-8"}
            url = "https://of.swu.edu.cn/gateway/fighter-baida/api/form-instance/save"
            params = {"formId": formid, "isSubmitProcess": False}
            dormitory = get_dormitory(token, timeout, session=session)["data"]["columnList"]
            payload = {
                "id": id,
                "formId": formid,
                "tsrq": time.strftime("%Y-%m-%d"),
                "xh": get_student_id(token, session=session),
                "qdsj": ["21:00", "23:30"],
                "qsqddd": dormitory[1]["value"],
                "qdbj": dormitory[2]["value"],
                "qddz": {
                    "latitude": dormitory[0]["latitude"],
                    "longitude": dormitory[0]["longitude"],
                    "address": dormitory[1]["value"],
                    "netType": "wifi",
                    "operatorType": "unknown",
                    "imei": "imei",
                    "time": int(time.time() * 1000),
                    "provider": "lbs",
                    "isFromMock": False,
                    "isGpsEnabled": True,
                    "isWifiEnabled": True,
                    "isMobileEnabled": False,
                    "isOffset": True,
                    "cityAdCode": "023",
                    "districtAdCode": "500109",
                    "isArea": True,
                    "tip": "当前在签到范围内"
                }
            }
            response = request_with_retry("POST", url, headers=headers, params=params, data=json.dumps(payload), timeout=timeout, session=session)
            return response.json()["data"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            return 4
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error(f"提交签到时接口返回结构异常: {e}")
            return 10
        except Exception as e:
            logger.error(f"提交签到异常: {e}")
            return 10

    try:
        token = get_token(username, password, timeout, session=session, force_login=force_login)
    except Exception as e:
        reason = getattr(e, "reason", "unknown")
        status = LOGIN_REASON_STATUS.get(reason, 11 if "token" in str(e).lower() else 10)
        logger.error(f"登录失败（{STATUS_MESSAGES.get(status, '未知原因')}）: {e}")
        return status

    try:
        if vacation_enable(token, timeout, session=session):
            return 5
        transition_today = get_transition_today(token, timeout, session=session)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        logger.error(f"学校接口连接失败或超时: {e}")
        return 4
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.error(f"学校接口返回结构异常: {e}")
        return 10
    except Exception as e:
        logger.error(f"学校接口请求异常: {e}")
        return 10

    if not transition_today:
        return 0
    if transition_today["qdzt"] == "已签到":
        return 2
    post_result = checkin_post(token, timeout, session=session, transition_today=transition_today)
    if post_result in (4, 10):
        return post_result
    return 1


def validate_accounts(accounts):
    if not isinstance(accounts, list):
        raise ValueError("账号配置必须是 JSON 数组格式（List）")
    
    validated = []
    for idx, acc in enumerate(accounts, 1):
        if not isinstance(acc, dict):
            raise ValueError(f"第 {idx} 个账号配置格式错误：应为 JSON 对象（键值对）")
        
        username = acc.get("username")
        password = acc.get("password")
        
        if username is None or password is None:
            raise ValueError(f"第 {idx} 个账号配置不完整：必须包含 'username' 和 'password' 字段")
        
        if not isinstance(username, (str, int, float)) or isinstance(username, bool):
            raise ValueError(f"第 {idx} 个账号配置类型错误：'username' 必须是字符串或数字类型")
        if not isinstance(password, (str, int, float)) or isinstance(password, bool):
            raise ValueError(f"第 {idx} 个账号配置类型错误：'password' 必须是字符串或数字类型")
            
        username_str = str(username).strip()
        password_str = str(password).strip()
        
        if not username_str:
            raise ValueError(f"第 {idx} 个账号配置错误：'username' 不能为空")
        if not password_str:
            raise ValueError(f"第 {idx} 个账号配置错误：'password' 不能为空")
            
        validated.append({"username": username_str, "password": password_str})
        
    return validated


def mask_account(value: str) -> str:
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def configured_push_channels():
    channels = []
    env_to_name = [
        ("PUSH_DINGTALK_TOKEN", "DingTalk"),
        ("PUSH_QYWX_KEY", "WeChat Work"),
        ("PUSH_BARK_KEY", "Bark"),
        ("PUSH_SERVERCHAN_KEY", "ServerChan"),
        ("PUSH_PUSHDEER_KEY", "PushDeer"),
    ]
    for env_name, channel_name in env_to_name:
        if os.getenv(env_name, "").strip():
            channels.append(channel_name)
    return channels


def check_dependency(name, import_name=None):
    if name == "requests" and REQUESTS_IMPORT_ERROR is not None:
        return False, str(REQUESTS_IMPORT_ERROR)
    try:
        __import__(import_name or name)
        return True, None
    except Exception as exc:
        return False, str(exc)


def run_config_check(cli_username=None, cli_password=None):
    print("SWU 自动打卡配置检查")
    print("=" * 28)
    print(f"配置目录：{CONFIG_DIR}")

    accounts = []
    source = None
    errors = []

    if cli_username or cli_password:
        if not cli_username or not cli_password:
            errors.append("命令行账号参数不完整：-u/--username 和 -p/--password 必须同时提供。")
        else:
            try:
                accounts = validate_accounts([{"username": cli_username, "password": cli_password}])
                source = "命令行参数"
            except ValueError as exc:
                errors.append(f"命令行账号参数无效：{exc}")

    users_path = users_config_path()
    if not accounts and os.path.exists(users_path):
        try:
            with open(users_path, "r", encoding="utf-8") as f:
                accounts = validate_accounts(json.load(f))
            source = "users.json"
        except json.JSONDecodeError as exc:
            errors.append(f"users.json 不是合法 JSON：第 {exc.lineno} 行，第 {exc.colno} 列，{exc.msg}")
        except Exception as exc:
            errors.append(f"users.json 读取失败：{exc}")

    swu_users_env = os.getenv("SWU_USERS", "").strip()
    if not accounts and swu_users_env:
        try:
            accounts = validate_accounts(json.loads(swu_users_env))
            source = "环境变量 SWU_USERS"
        except json.JSONDecodeError as exc:
            errors.append(f"环境变量 SWU_USERS 不是合法 JSON：{exc}")
        except Exception as exc:
            errors.append(f"环境变量 SWU_USERS 无效：{exc}")

    if not accounts:
        user = os.getenv("SWU_USERNAME", "").strip()
        pwd = os.getenv("SWU_PASSWORD", "").strip()
        if user or pwd:
            try:
                accounts = validate_accounts([{"username": user, "password": pwd}])
                source = "环境变量 SWU_USERNAME/SWU_PASSWORD"
            except Exception as exc:
                errors.append(f"单账号环境变量无效：{exc}")

    if accounts:
        print(f"[OK] 账号配置：{source}，共 {len(accounts)} 个账号")
        for idx, account in enumerate(accounts, 1):
            print(f"     {idx}. {mask_account(account['username'])}")
    else:
        print("[FAIL] 账号配置：未找到可用账号")
        print("       请配置 users.json、SWU_USERS，或 SWU_USERNAME/SWU_PASSWORD。")

    token_cache_path = os.path.join(CONFIG_DIR, ".token_cache.json")
    if os.path.exists(token_cache_path):
        try:
            with open(token_cache_path, "r", encoding="utf-8") as f:
                cached_tokens = json.load(f)
            print(f"[OK] Token 缓存：已存在，包含 {len(cached_tokens)} 个账号")
        except Exception as exc:
            print(f"[WARN] Token 缓存：文件存在但无法读取，后续会自动重新登录 ({exc})")
    else:
        print("[INFO] Token 缓存：未发现，首次运行会通过浏览器登录")

    channels = configured_push_channels()
    if channels:
        print(f"[OK] 推送配置：已配置 {', '.join(channels)}")
    else:
        print("[INFO] 推送配置：未配置，运行结束后只输出日志")

    try:
        proxy_description = describe_proxy_config()
        proxy_ok, proxy_err = validate_proxy_config()
    except Exception as exc:
        proxy_description = f"读取失败 ({exc})"
        proxy_ok, proxy_err = False, str(exc)
    if proxy_description == "未配置":
        print("[INFO] 学校官网代理：未配置，直接访问学校接口")
    elif not proxy_ok:
        print(f"[FAIL] 学校官网代理：{proxy_err}")
    else:
        print(f"[OK] 学校官网代理：{proxy_description}")

    if REQUESTS_IMPORT_ERROR is None:
        connectivity_ok, connectivity_msg = check_school_connectivity(timeout=5)
        if connectivity_ok:
            print(f"[OK] 学校官网连通性：{connectivity_msg}")
        else:
            print(f"[WARN] 学校官网连通性：{connectivity_msg}")
            if proxy_description == "未配置":
                print("       如果服务器在海外，通常需要让服务器已有的 HTTPS_PROXY/ALL_PROXY 指向中国大陆代理出口，或在菜单中配置 SWU_PROXY_URL。")

    deps = [
        ("requests", "requests"),
        ("PySocks", "socks"),
        ("playwright", "playwright.sync_api"),
        ("ddddocr", "ddddocr"),
        ("python-dotenv", "dotenv"),
    ]
    deps_ok = True
    for label, import_name in deps:
        ok, err = check_dependency(label, import_name)
        if ok:
            print(f"[OK] 依赖：{label}")
        else:
            deps_ok = False
            print(f"[FAIL] 依赖：{label} 未安装或不可用 ({err})")

    max_workers_env = os.getenv("SWU_MAX_WORKERS", "").strip()
    if max_workers_env and not max_workers_env.isdigit():
        print(f"[WARN] 并发配置：SWU_MAX_WORKERS={max_workers_env} 不是正整数，将使用默认值 3")
    else:
        print(f"[OK] 并发配置：最大线程数 {max_workers_env or '3'}")

    if errors:
        print("\n需要处理的问题：")
        for err in errors:
            print(f"- {err}")

    if accounts and deps_ok and not errors and proxy_ok:
        print("\n配置检查通过。")
        return 0

    print("\n配置检查未通过，请先处理上面的 FAIL 项。")
    return 1


def users_config_path():
    return os.path.join(CONFIG_DIR, "users.json")


def env_config_path():
    return os.path.join(CONFIG_DIR, ".env")


def load_users_file():
    path = users_config_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return validate_accounts(data)


def save_users_file(accounts):
    accounts = validate_accounts(accounts)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(users_config_path(), "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
        f.write("\n")


def set_env_value(key, value):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.environ[key] = value
    path = env_config_path()
    lines = []
    found = False
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    updated = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)

    if not found:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(updated).rstrip() + "\n")


def unset_env_value(*keys):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    for key in keys:
        os.environ.pop(key, None)
    path = env_config_path()
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    key_set = set(keys)
    updated = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip()
            if name in key_set:
                continue
        updated.append(line)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(updated).rstrip() + "\n")


def prompt_non_empty(label):
    while True:
        value = input(label).strip()
        if value:
            return value
        print("输入不能为空，请重新输入。")


def prompt_password(label="请输入密码："):
    import getpass
    while True:
        value = getpass.getpass(label).strip()
        if value:
            return value
        print("密码不能为空，请重新输入。")


def pause_menu():
    input("\n按 Enter 返回菜单...")


def list_users_for_menu(accounts):
    if not accounts:
        print("当前 users.json 中没有账号。")
        return
    for idx, account in enumerate(accounts, 1):
        print(f"{idx}. {mask_account(account['username'])}")


def select_account(accounts, action_name):
    if not accounts:
        print("当前 users.json 中没有账号。")
        return None
    list_users_for_menu(accounts)
    raw = input(f"请选择要{action_name}的账号编号：").strip()
    if not raw.isdigit():
        print("请输入数字编号。")
        return None
    index = int(raw)
    if index < 1 or index > len(accounts):
        print("账号编号不存在。")
        return None
    return index - 1


def menu_add_account():
    accounts = load_users_file()
    username = prompt_non_empty("请输入校园网账号：")
    if any(acc["username"] == username for acc in accounts):
        print("这个账号已经存在。")
        return
    password = prompt_password()
    accounts.append({"username": username, "password": password})
    save_users_file(accounts)
    print(f"已添加账号 {mask_account(username)} 到 users.json。")


def menu_remove_account():
    accounts = load_users_file()
    index = select_account(accounts, "删除")
    if index is None:
        return
    account = accounts[index]
    confirm = input(f"确认删除账号 {mask_account(account['username'])}？输入 yes 确认：").strip().lower()
    if confirm != "yes":
        print("已取消删除。")
        return
    removed = accounts.pop(index)
    save_users_file(accounts)
    print(f"已删除账号 {mask_account(removed['username'])}。")


def menu_update_password():
    accounts = load_users_file()
    index = select_account(accounts, "修改密码")
    if index is None:
        return
    accounts[index]["password"] = prompt_password("请输入新密码：")
    save_users_file(accounts)
    print(f"已更新账号 {mask_account(accounts[index]['username'])} 的密码。")


def menu_set_workers():
    raw = prompt_non_empty("请输入最大并发线程数（建议 1-3）：")
    if not raw.isdigit() or int(raw) < 1:
        print("并发线程数必须是正整数。")
        return
    set_env_value("SWU_MAX_WORKERS", raw)
    print(f"已写入 .env：SWU_MAX_WORKERS={raw}")


def menu_set_push():
    print("\n推送配置")
    print("1. 钉钉机器人")
    print("2. 企业微信群机器人")
    print("3. Bark")
    print("4. Server 酱")
    print("5. PushDeer")
    print("0. 返回")
    choice = input("请选择：").strip()

    if choice == "1":
        token = prompt_non_empty("PUSH_DINGTALK_TOKEN：")
        secret = input("PUSH_DINGTALK_SECRET（可留空）：").strip()
        set_env_value("PUSH_DINGTALK_TOKEN", token)
        set_env_value("PUSH_DINGTALK_SECRET", secret)
        print("已保存钉钉推送配置。")
    elif choice == "2":
        key = prompt_non_empty("PUSH_QYWX_KEY：")
        set_env_value("PUSH_QYWX_KEY", key)
        print("已保存企业微信推送配置。")
    elif choice == "3":
        key = prompt_non_empty("PUSH_BARK_KEY：")
        url = input("PUSH_BARK_URL（默认 https://api.day.app）：").strip() or "https://api.day.app"
        set_env_value("PUSH_BARK_KEY", key)
        set_env_value("PUSH_BARK_URL", url)
        print("已保存 Bark 推送配置。")
    elif choice == "4":
        key = prompt_non_empty("PUSH_SERVERCHAN_KEY：")
        set_env_value("PUSH_SERVERCHAN_KEY", key)
        print("已保存 Server 酱推送配置。")
    elif choice == "5":
        key = prompt_non_empty("PUSH_PUSHDEER_KEY：")
        set_env_value("PUSH_PUSHDEER_KEY", key)
        print("已保存 PushDeer 推送配置。")
    elif choice == "0":
        return
    else:
        print("无效选项。")


def menu_set_proxy():
    print("\n学校官网代理配置")
    print("用于海外 VPS 通过中国大陆代理出口访问学校官网。")
    print("支持 http://、https://、socks5:// 形式；如果省略协议，默认按 http:// 处理。")
    print(f"当前配置：{describe_proxy_config()}")
    print("1. 设置或修改代理")
    print("2. 清除代理配置")
    print("0. 返回")
    choice = input("请选择：").strip()

    if choice == "1":
        proxy_url = prompt_non_empty("SWU_PROXY_URL（例如 http://1.2.3.4:7890）：")
        username = input("SWU_PROXY_USERNAME（无认证可留空）：").strip()
        password = ""
        if username:
            password = prompt_password("SWU_PROXY_PASSWORD：")
        set_env_value("SWU_PROXY_URL", proxy_url)
        set_env_value("SWU_PROXY_USERNAME", username)
        set_env_value("SWU_PROXY_PASSWORD", password)
        print(f"已保存学校官网代理配置：{describe_proxy_config()}")
        print("下次运行打卡时，浏览器登录和学校接口请求都会使用该代理。")
    elif choice == "2":
        confirm = input("确认清除学校官网代理配置？输入 yes 确认：").strip().lower()
        if confirm != "yes":
            print("已取消清除。")
            return
        unset_env_value("SWU_PROXY_URL", "SWU_PROXY_USERNAME", "SWU_PROXY_PASSWORD")
        print("已清除学校官网代理配置。")
    elif choice == "0":
        return
    else:
        print("无效选项。")


def menu_show_paths():
    paths = [
        ("配置目录", CONFIG_DIR),
        ("账号文件", users_config_path()),
        ("环境变量文件", env_config_path()),
        ("Token 缓存", os.path.join(CONFIG_DIR, ".token_cache.json")),
        ("运行锁", os.path.join(CONFIG_DIR, ".run.lock")),
    ]
    for label, path in paths:
        exists = "存在" if os.path.exists(path) else "不存在"
        print(f"{label}: {path} ({exists})")


def menu_clear_token_cache():
    cache_path = os.path.join(CONFIG_DIR, ".token_cache.json")
    if not os.path.exists(cache_path):
        print("当前没有 Token 缓存。")
        return
    confirm = input("确认清除 Token 缓存？下次运行会重新登录。输入 yes 确认：").strip().lower()
    if confirm != "yes":
        print("已取消清除。")
        return
    os.remove(cache_path)
    print("已清除 Token 缓存。")


def menu_test_push():
    try:
        from notify import send_push
    except Exception as exc:
        print(f"无法加载推送模块：{exc}")
        return

    title = "SWU 自动打卡测试通知"
    content = f"这是一条测试推送。\n配置目录：{CONFIG_DIR}\n发送时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
    print("正在发送测试推送...")
    send_push(title, content)
    print("测试推送已触发，请检查对应平台是否收到消息。")


def menu_run_checkin_once():
    confirm = input("确认立即执行一次打卡？输入 yes 确认：").strip().lower()
    if confirm != "yes":
        print("已取消执行。")
        return
    import subprocess
    env = os.environ.copy()
    env["SWU_CONFIG_DIR"] = CONFIG_DIR
    print("开始执行打卡，完成前请不要关闭终端...")
    result = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env)
    print(f"打卡进程已结束，退出码：{result.returncode}")


def run_menu():
    while True:
        print("\nSWU 自动打卡配置菜单")
        print("=" * 24)
        print("1. 查看配置检查")
        print("2. 查看 users.json 账号")
        print("3. 添加账号")
        print("4. 删除账号")
        print("5. 修改账号密码")
        print("6. 设置并发线程数")
        print("7. 配置推送通道")
        print("8. 查看配置文件路径")
        print("9. 清除 Token 缓存")
        print("10. 测试推送通道")
        print("11. 立即执行一次打卡")
        print("12. 配置学校官网代理")
        print("0. 退出")

        try:
            choice = input("请输入数字选项：").strip()
            if choice == "1":
                run_config_check()
                pause_menu()
            elif choice == "2":
                list_users_for_menu(load_users_file())
                pause_menu()
            elif choice == "3":
                menu_add_account()
                pause_menu()
            elif choice == "4":
                menu_remove_account()
                pause_menu()
            elif choice == "5":
                menu_update_password()
                pause_menu()
            elif choice == "6":
                menu_set_workers()
                pause_menu()
            elif choice == "7":
                menu_set_push()
                pause_menu()
            elif choice == "8":
                menu_show_paths()
                pause_menu()
            elif choice == "9":
                menu_clear_token_cache()
                pause_menu()
            elif choice == "10":
                menu_test_push()
                pause_menu()
            elif choice == "11":
                menu_run_checkin_once()
                pause_menu()
            elif choice == "12":
                menu_set_proxy()
                pause_menu()
            elif choice == "0":
                print("已退出菜单。")
                return 0
            else:
                print("无效选项，请输入菜单中的数字。")
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"配置文件格式有误：{exc}")
            pause_menu()
        except KeyboardInterrupt:
            print("\n已退出菜单。")
            return 1
        except EOFError:
            print("\n已退出菜单。")
            return 0


def acquire_run_lock():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lock_path = os.path.join(CONFIG_DIR, ".run.lock")
    stale_seconds = 7200
    stale_env = os.getenv("SWU_LOCK_STALE_SECONDS", "").strip()
    if stale_env.isdigit():
        stale_seconds = int(stale_env)

    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < stale_seconds:
            logger.warning(f"检测到已有任务正在运行，跳过本次执行。锁文件：{lock_path}")
            return None
        logger.warning(f"检测到过期锁文件，已清理：{lock_path}")
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        logger.warning(f"检测到已有任务正在运行，跳过本次执行。锁文件：{lock_path}")
        return None

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False))
        f.write("\n")

    return lock_path


def release_run_lock(lock_path):
    if not lock_path:
        return
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"清理运行锁失败：{exc}")


if __name__ == "__main__":
    import argparse
    setup_logging()

    # 命令行参数解析
    parser = argparse.ArgumentParser(description="西南大学自动打卡脚本")
    parser.add_argument("-u", "--username", type=str, help="临时的校园网账号（若配置，将忽略 users.json 和环境变量）")
    parser.add_argument("-p", "--password", type=str, help="临时的校园网密码")
    parser.add_argument("-f", "--force-login", action="store_true", help="强制通过浏览器重新登录（忽略 Token 缓存）")
    parser.add_argument("--check-config", action="store_true", help="检查账号、依赖、推送和缓存配置，不执行登录或打卡")
    parser.add_argument("-m", "--menu", action="store_true", help="打开数字配置菜单，用于添加账号或修改配置")
    parser.add_argument("--no-lock", action="store_true", help="跳过运行锁检查，通常不建议在定时任务中使用")
    args = parser.parse_args()

    if args.menu:
        raise SystemExit(run_menu())

    if args.check_config:
        raise SystemExit(run_config_check(args.username, args.password))

    if GET_INFO_IMPORT_ERROR is not None or REQUESTS_IMPORT_ERROR is not None:
        missing = GET_INFO_IMPORT_ERROR or REQUESTS_IMPORT_ERROR
        logger.error(f"依赖加载失败：{missing}")
        logger.error("请先安装依赖，或运行 --check-config 查看当前环境状态。")
        raise SystemExit(1)

    proxy_ok, proxy_err = validate_proxy_config()
    if not proxy_ok:
        logger.error(f"学校官网代理配置无效：{proxy_err}")
        raise SystemExit(1)

    connectivity_ok, connectivity_msg = check_school_connectivity(timeout=5)
    if connectivity_ok:
        logger.info(f"学校官网连通性检查通过：{connectivity_msg}")
    else:
        logger.warning(f"学校官网连通性检查失败：{connectivity_msg}")
        if has_school_proxy_config():
            logger.warning("已检测到代理配置，请检查代理出口是否位于中国大陆内、代理地址是否可达、认证信息是否正确。")
        else:
            logger.error("当前没有可用的学校官网代理配置，且直连学校官网失败。")
            logger.error("如果服务器在海外且服务端也不提供任何中国大陆出口、代理或隧道，脚本无法凭空访问学校官网。")
            logger.error("请换用中国大陆内服务器运行，或提供一个可信的中国大陆网络出口。")
            raise SystemExit(1)

    run_lock_path = None
    if not args.no_lock:
        run_lock_path = acquire_run_lock()
        if run_lock_path is None:
            raise SystemExit(0)
        atexit.register(release_run_lock, run_lock_path)
    
    logger.info("开始执行签到...")
    accounts = []
    
    # 0. 优先使用命令行参数指定的账号密码
    if args.username or args.password:
        if not args.username or not args.password:
            logger.error("通过命令行参数打卡时，用户名 -u 和密码 -p 必须同时配置！")
            raise SystemExit(1)
        try:
            raw_accounts = [{"username": args.username, "password": args.password}]
            accounts = validate_accounts(raw_accounts)
            logger.info("已从命令行参数读取并校验通过账号信息。")
        except ValueError as e:
            logger.error(f"校验命令行参数失败: {e}")
            raise SystemExit(1)

    # 1. 尝试从当前目录的 users.json 读取
    if not accounts:
        config_path = users_config_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    raw_accounts = json.load(f)
                accounts = validate_accounts(raw_accounts)
                logger.info(f"已从 users.json 读取并校验通过 {len(accounts)} 个账号信息。")
            except json.JSONDecodeError as e:
                logger.error(f"解析 users.json 失败：JSON 语法错误 (第 {e.lineno} 行，第 {e.colno} 列): {e.msg}")
                raise SystemExit(1)
            except ValueError as e:
                logger.error(f"校验 users.json 配置失败: {e}")
                raise SystemExit(1)
            except Exception as e:
                logger.error(f"读取 users.json 失败: {e}")
                raise SystemExit(1)
            
    # 2. 尝试从环境变量 SWU_USERS 读取 (JSON 格式)
    if not accounts:
        swu_users_env = os.getenv("SWU_USERS", "").strip()
        if swu_users_env:
            try:
                raw_accounts = json.loads(swu_users_env)
                accounts = validate_accounts(raw_accounts)
                logger.info(f"已从环境变量 SWU_USERS 读取并校验通过 {len(accounts)} 个账号信息。")
            except json.JSONDecodeError as e:
                logger.error(f"解析环境变量 SWU_USERS 失败：JSON 语法错误: {e}")
                raise SystemExit(1)
            except ValueError as e:
                logger.error(f"校验环境变量 SWU_USERS 配置失败: {e}")
                raise SystemExit(1)
            except Exception as e:
                logger.error(f"读取环境变量 SWU_USERS 失败: {e}")
                raise SystemExit(1)
                
    # 3. 回退到单账号环境变量 SWU_USERNAME / SWU_PASSWORD
    if not accounts:
        user = os.getenv("SWU_USERNAME", "").strip()
        pwd = os.getenv("SWU_PASSWORD", "").strip()
        if user or pwd:
            try:
                raw_accounts = [{"username": user, "password": pwd}]
                accounts = validate_accounts(raw_accounts)
                logger.info("已从单账号环境变量读取并校验通过账号信息。")
            except ValueError as e:
                logger.error(f"校验单账号环境变量失败: {e}")
                raise SystemExit(1)

    # 4. 如果所有方式都没有配置账号，且在交互式终端中运行，启动向导
    if not accounts:
        if sys.stdin.isatty():
            try:
                print("检测到当前未配置任何账号信息。")
                choice = input("是否立即启动交互式配置向导创建 users.json？(y/n): ").strip().lower()
                if choice in ['y', 'yes']:
                    import getpass
                    new_accounts = []
                    while True:
                        print(f"\n--- 添加第 {len(new_accounts) + 1} 个账号 ---")
                        uname = input("请输入校园网账号（学工号）: ").strip()
                        if not uname:
                            print("账号不能为空，请重新输入。")
                            continue
                        pwd = getpass.getpass("请输入密码（输入已隐藏，直接回车即可）: ").strip()
                        if not pwd:
                            print("密码不能为空，请重新输入。")
                            continue
                        new_accounts.append({"username": uname, "password": pwd})
                        more = input("是否继续添加账号？(y/n): ").strip().lower()
                        if more not in ['y', 'yes']:
                            break
                    
                    config_path = users_config_path()
                    try:
                        os.makedirs(CONFIG_DIR, exist_ok=True)
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(new_accounts, f, ensure_ascii=False, indent=2)
                        logger.info(f"配置成功！已生成 users.json，共配置 {len(new_accounts)} 个账号。")
                        accounts = validate_accounts(new_accounts)
                    except Exception as ex:
                        logger.error(f"写入配置文件失败: {ex}")
                        raise SystemExit(1)
            except (KeyboardInterrupt, EOFError):
                print("\n已取消配置向导。")
                raise SystemExit(1)
            
    if not accounts:
        logger.error("未配置账号信息！请提供以下之一：\n  1. 同目录下创建 users.json\n  2. 设置环境变量 SWU_USERS (JSON 格式)\n  3. 设置环境变量 SWU_USERNAME 和 SWU_PASSWORD")
        raise SystemExit(1)
        
    message_map = STATUS_MESSAGES
    
    def run_account_checkin(idx, acc, total_accounts):
        username = acc["username"]
        password = acc["password"]

        logger.info(f"[{idx}/{total_accounts}] 开始为账号 {username} 执行签到...")
        try:
            result = check_in(username, password, force_login=args.force_login)
            msg = message_map.get(result, '未知状态')
            logger.info(f"[{idx}/{total_accounts}] 账号 {username} 签到结果: {msg}")
            return idx, username, result, None
        except Exception as e:
            logger.error(f"[{idx}/{total_accounts}] 账号 {username} 签到执行异常: {e}")
            return idx, username, -2, str(e)

    success_count = 0
    failed_count = 0
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    max_workers_env = os.getenv("SWU_MAX_WORKERS", "").strip()
    max_workers = 3
    if max_workers_env.isdigit():
        max_workers = int(max_workers_env)
        
    logger.info(f"并发执行：最大线程数 = {max_workers}")
    
    results_summary = []
    
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, acc in enumerate(accounts, 1):
            future = executor.submit(run_account_checkin, idx, acc, len(accounts))
            futures[future] = acc.get("username", "").strip()
            
        for future in as_completed(futures):
            username = futures[future]
            try:
                idx, user, result, err = future.result()
                if err is not None:
                    status_msg = f"执行异常: {err}"
                    is_ok = False
                else:
                    status_msg = message_map.get(result, '未知状态')
                    is_ok = (result in [0, 1, 2, 5])
                
                results_summary.append((user, status_msg, is_ok))
                if is_ok:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"线程执行异常 ({username}): {e}")
                results_summary.append((username, f"线程执行异常: {e}", False))
                failed_count += 1
                
    summary_title = "西南大学自动签到任务通知"
    summary_content = f"打卡执行完毕！成功: {success_count} 个，失败: {failed_count} 个。\n\n打卡详情:"
    results_summary.sort(key=lambda x: x[0])
    for user, status, is_ok in results_summary:
        icon = "✅" if is_ok else "❌"
        summary_content += f"\n{icon} 账号 {user}: {status}"
        
    logger.info(f"\n{summary_content}")
    
    try:
        from notify import send_push
        send_push(summary_title, summary_content)
    except Exception as e:
        logger.error(f"发送消息推送异常: {e}")
