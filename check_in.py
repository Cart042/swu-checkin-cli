import time
import os
import logging
from get_info import *
import requests
from des import des

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

logger = logging.getLogger("swu.check_in")

def check_in(username: str, password: str, timeout: int = 10, force_login: bool = False):
    session = requests.Session()

    def vacation_enable(token, timeout, session):
        headers = {
            "fighter-auth-token": token
        }
        url = 'https://of.swu.edu.cn/gateway/fighter-baida/api/flow-ext/start-process-instance-by-key'
        params = {'processDefinitionKey': 'XSQJXJ'}
        response = request_with_retry("POST", url, headers=headers, params=params, json={}, timeout=timeout, session=session)
        if response.json()["code"] == 200 or response.json()["code"] == 1100:
            return 0
        else:
            return 1

    def checkin_post(token, timeout, session):
        try:
            transition_today = get_transition_today(token, timeout, session=session)
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

    try:
        token = get_token(username, password, timeout, session=session, force_login=force_login)
    except Exception as e:
        logger.error(f"登录异常: {e}")
        return 3
    if vacation_enable(token, timeout, session=session):
        return 5
    transition_today = get_transition_today(token, timeout, session=session)
    if not transition_today:
        return 0
    if transition_today["qdzt"] == "已签到":
        return 2
    post_result = checkin_post(token, timeout, session=session)
    if post_result == 4:
        return 4
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


if __name__ == "__main__":
    import json
    import argparse
    import sys
    setup_logging()

    # 命令行参数解析
    parser = argparse.ArgumentParser(description="西南大学自动打卡脚本")
    parser.add_argument("-u", "--username", type=str, help="临时的校园网账号（若配置，将忽略 users.json 和环境变量）")
    parser.add_argument("-p", "--password", type=str, help="临时的校园网密码")
    parser.add_argument("-f", "--force-login", action="store_true", help="强制通过浏览器重新登录（忽略 Token 缓存）")
    args = parser.parse_args()
    
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
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
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
                    
                    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
                    try:
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
        
    message_map = {
        0: "今日暂无签到任务。",
        1: "签到成功。",
        2: "今日已签到，无需重复操作。",
        3: "账号或密码验证失败，请检查后重试。",
        4: "连接错误或请求超时，请稍后重试。",
        5: "请假中，请检查是否有打卡任务。"
    }
    
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



