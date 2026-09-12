"""Interactive configuration menu.

The menu imports the check-in runtime only when a user chooses an action that
needs it.  Account and dotenv operations come from :mod:`swu_checkin.config`, so opening
the menu does not require Playwright, OCR, or requests to be importable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable

from . import config


def prompt_non_empty(label: str) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        print("输入不能为空，请重新输入。")


def prompt_password(label: str = "请输入密码：") -> str:
    import getpass

    while True:
        value = getpass.getpass(label)
        if value != "":
            return value
        print("密码不能为空，请重新输入。")


def pause_menu() -> None:
    input("\n按 Enter 返回菜单...")


def list_users_for_menu(accounts) -> None:
    if not accounts:
        print("当前 users.json 中没有账号。")
        return
    for idx, account in enumerate(accounts, 1):
        print(f"{idx}. {config.mask_account(account['username'])}")


def select_account(accounts, action_name: str):
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


def menu_add_account(
    config_dir=None,
    *,
    prompt_password_func: Callable[[str], str] | None = None,
) -> None:
    directory = config.get_config_dir(config_dir)
    accounts = config.load_users_file(directory)
    username = prompt_non_empty("请输入教务系统（校园网）账户名（不是学号）：")
    if any(account["username"] == username for account in accounts):
        print("这个账号已经存在。")
        return
    password = (prompt_password_func or prompt_password)("请输入密码：")
    accounts.append({"username": username, "password": password})
    config.save_users_file(accounts, directory)
    print(f"已添加账号 {config.mask_account(username)} 到 users.json。")


def create_accounts_wizard(
    config_dir=None,
    *,
    prompt_password_func: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    """Collect accounts interactively and persist them through config.py."""

    if not sys.stdin.isatty():
        return []
    print("检测到当前未配置任何账号信息。")
    choice = input("是否立即启动交互式配置向导创建 users.json？(y/n): ").strip().lower()
    if choice not in {"y", "yes"}:
        return []

    ask_password = prompt_password_func or prompt_password
    accounts: list[dict[str, str]] = []
    while True:
        print(f"\n--- 添加第 {len(accounts) + 1} 个账号 ---")
        username = input("请输入教务系统（校园网）账户名（不是学号）: ").strip()
        if not username:
            print("账号不能为空，请重新输入。")
            continue
        password = ask_password("请输入密码（输入已隐藏，直接回车即可）: ")
        if password == "":
            print("密码不能为空，请重新输入。")
            continue
        accounts.append({"username": username, "password": password})
        if input("是否继续添加账号？(y/n): ").strip().lower() not in {"y", "yes"}:
            break

    config.save_users_file(accounts, config.get_config_dir(config_dir))
    print(f"配置成功！已生成 users.json，共配置 {len(accounts)} 个账号。")
    return accounts


def menu_remove_account(config_dir=None) -> None:
    directory = config.get_config_dir(config_dir)
    accounts = config.load_users_file(directory)
    index = select_account(accounts, "删除")
    if index is None:
        return
    account = accounts[index]
    confirm = input(f"确认删除账号 {config.mask_account(account['username'])}？输入 yes 确认：").strip().lower()
    if confirm != "yes":
        print("已取消删除。")
        return
    removed = accounts.pop(index)
    config.save_users_file(accounts, directory)
    print(f"已删除账号 {config.mask_account(removed['username'])}。")


def menu_update_password(
    config_dir=None,
    *,
    prompt_password_func: Callable[[str], str] | None = None,
) -> None:
    directory = config.get_config_dir(config_dir)
    accounts = config.load_users_file(directory)
    index = select_account(accounts, "修改密码")
    if index is None:
        return
    accounts[index]["password"] = (prompt_password_func or prompt_password)("请输入新密码：")
    config.save_users_file(accounts, directory)
    print(f"已更新账号 {config.mask_account(accounts[index]['username'])} 的密码。")


def menu_set_workers(config_dir=None) -> None:
    spec = config.RUNTIME_PARAMETER_BY_KEY["max_workers"]
    raw = prompt_non_empty(f"请输入最大并发线程数（{spec.minimum}-{spec.maximum}）：")
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        value = None
    if value is None or not spec.minimum <= value <= spec.maximum:
        print(f"并发线程数必须是 {spec.minimum}-{spec.maximum} 之间的整数。")
        return
    config.set_env_value(spec.env_name, raw, config_dir=config_dir)
    print(f"已写入 .env：{spec.env_name}={raw}")


def menu_set_telegram(
    config_dir=None,
    *,
    prompt_password_func: Callable[[str], str] | None = None,
) -> None:
    """Set or clear Telegram credentials without sending a message."""

    token_configured = bool(os.getenv("PUSH_TELEGRAM_BOT_TOKEN", "").strip())
    chat_configured = bool(os.getenv("PUSH_TELEGRAM_CHAT_ID", "").strip())
    if token_configured or chat_configured:
        print("\nTelegram 当前已有配置。")
    print("1. 设置或修改 Telegram")
    print("2. 清除 Telegram 配置")
    print("0. 返回")
    choice = input("请选择：").strip()

    if choice == "1":
        ask_password = prompt_password_func or prompt_password
        token = ask_password("PUSH_TELEGRAM_BOT_TOKEN：")
        chat_id = prompt_non_empty("PUSH_TELEGRAM_CHAT_ID：")
        config.set_env_value("PUSH_TELEGRAM_BOT_TOKEN", token, config_dir=config_dir)
        config.set_env_value("PUSH_TELEGRAM_CHAT_ID", chat_id, config_dir=config_dir)
        print("已保存 Telegram 推送配置。")
    elif choice == "2":
        confirm = input("确认清除 Telegram 配置？输入 yes 确认：").strip().lower()
        if confirm != "yes":
            print("已取消清除。")
            return
        config.unset_env_value(
            "PUSH_TELEGRAM_BOT_TOKEN",
            "PUSH_TELEGRAM_CHAT_ID",
            config_dir=config_dir,
        )
        print("已清除 Telegram 推送配置。")
    elif choice != "0":
        print("无效选项。")


def menu_set_push(config_dir=None, *, prompt_password_func=None) -> None:
    print("\n推送配置")
    for index, channel in enumerate(config.PUSH_CHANNELS, 1):
        print(f"{index}. {channel.menu_label}")
    print("0. 返回")
    choice = input("请选择：").strip()

    if choice == "0":
        return
    try:
        index = int(choice, 10)
    except (TypeError, ValueError):
        index = 0
    if not 1 <= index <= len(config.PUSH_CHANNELS):
        print("无效选项。")
        return

    channel = config.PUSH_CHANNELS[index - 1]
    if channel.name == "Telegram":
        menu_set_telegram(config_dir, prompt_password_func=prompt_password_func)
        return

    for env_name in channel.required_env:
        value = prompt_non_empty(f"{env_name}：")
        config.set_env_value(env_name, value, config_dir=config_dir)

    for env_name in channel.optional_env:
        if env_name == "PUSH_BARK_URL":
            value = input(f"{env_name}（默认 https://api.day.app）：").strip() or "https://api.day.app"
        else:
            value = input(f"{env_name}（可留空）：").strip()
        config.set_env_value(env_name, value, config_dir=config_dir)
    print(f"已保存{channel.menu_label}推送配置。")


def menu_show_paths(config_dir=None) -> None:
    directory = config.get_config_dir(config_dir)
    paths = [
        ("配置目录", directory),
        ("账号文件", config.users_config_path(directory)),
        ("环境变量文件", config.env_config_path(directory)),
        ("Token 缓存", os.path.join(directory, ".token_cache.json")),
        ("运行锁", os.path.join(directory, ".run.lock")),
    ]
    for label, path in paths:
        exists = "存在" if os.path.exists(path) else "不存在"
        print(f"{label}: {path} ({exists})")


def menu_clear_token_cache(config_dir=None) -> None:
    directory = config.get_config_dir(config_dir)
    cache_path = os.path.join(directory, ".token_cache.json")
    if not os.path.exists(cache_path):
        print("当前没有 Token 缓存。")
        return
    confirm = input("确认清除 Token 缓存？下次运行会重新登录。输入 yes 确认：").strip().lower()
    if confirm != "yes":
        print("已取消清除。")
        return
    os.remove(cache_path)
    print("已清除 Token 缓存。")


def menu_test_push(config_dir=None) -> None:
    try:
        from .notify import send_push
    except Exception as exc:
        print(f"无法加载推送模块：{exc}")
        return
    title = "SWU 自动打卡测试通知"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    content = f"这是一条测试推送。\n配置目录：{config.get_config_dir(config_dir)}\n发送时间：{timestamp}"
    print("正在发送测试推送...")
    send_push(title, content)
    print("测试推送已触发，请检查对应平台是否收到消息。")


def _checkin_command(script_path: str | None) -> list[str]:
    """Return the command that runs one check-in in a child process.

    The CLI passes its own path, which is the exact entry point that is being
    used right now.  Standalone menu callers fall back to the ``check_in.py``
    compatibility entry next to the project root, or to ``python -m
    swu_checkin`` for an installed package.
    """

    if script_path:
        return [sys.executable, os.path.abspath(script_path)]
    compatibility_entry = os.path.join(config.BASE_DIR, "check_in.py")
    if os.path.exists(compatibility_entry):
        return [sys.executable, compatibility_entry]
    return [sys.executable, "-m", "swu_checkin"]


def menu_run_checkin_once(config_dir=None, script_path=None) -> None:
    if input("确认立即执行一次打卡？输入 yes 确认：").strip().lower() != "yes":
        print("已取消执行。")
        return
    directory = config.get_config_dir(config_dir)
    env = os.environ.copy()
    env["SWU_CONFIG_DIR"] = directory
    print("开始执行打卡，完成前请不要关闭终端...")
    result = subprocess.run(_checkin_command(script_path), env=env)
    print(f"打卡进程已结束，退出码：{result.returncode}")


def run_menu(
    config_dir=None,
    *,
    config_check=None,
    script_path=None,
    prompt_password_func=None,
) -> int:
    """Run the numeric menu; callbacks keep the module independent of CLI code."""

    directory = config.get_config_dir(config_dir)
    if config_check is None:
        from .cli import run_config_check

        config_check = run_config_check

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
        print("0. 退出")
        try:
            choice = input("请输入数字选项：").strip()
            if choice == "1":
                config_check(config_dir=directory)
                pause_menu()
            elif choice == "2":
                list_users_for_menu(config.load_users_file(directory))
                pause_menu()
            elif choice == "3":
                menu_add_account(directory, prompt_password_func=prompt_password_func)
                pause_menu()
            elif choice == "4":
                menu_remove_account(directory)
                pause_menu()
            elif choice == "5":
                menu_update_password(directory, prompt_password_func=prompt_password_func)
                pause_menu()
            elif choice == "6":
                menu_set_workers(directory)
                pause_menu()
            elif choice == "7":
                menu_set_push(directory, prompt_password_func=prompt_password_func)
                pause_menu()
            elif choice == "8":
                menu_show_paths(directory)
                pause_menu()
            elif choice == "9":
                menu_clear_token_cache(directory)
                pause_menu()
            elif choice == "10":
                menu_test_push(directory)
                pause_menu()
            elif choice == "11":
                menu_run_checkin_once(directory, script_path=script_path)
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
