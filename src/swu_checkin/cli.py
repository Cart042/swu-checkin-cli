"""SWU check-in command-line interface.

Account configuration is provided by :mod:`swu_checkin.config` and the
interactive menu by :mod:`swu_checkin.menu`.  The browser/API service is
imported only when an actual check-in or an explicit network diagnostic is
requested.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import importlib.util
import json
import logging
import os
import time

from . import config

# ``LOGIN_REASON_STATUS`` 仍然从本模块导出，历史调用方按旧路径导入它。
from .status import (  # noqa: F401 - LOGIN_REASON_STATUS 继续从本模块导出
    LOGIN_REASON_STATUS,
    STATUS_MESSAGES,
    CheckinStatus,
)

BASE_DIR = config.BASE_DIR
# Kept as a public setting for deployments and callers that override it.
CONFIG_DIR = config.CONFIG_DIR
logger = logging.getLogger("swu.check_in")

RETRYABLE_STATUSES = {
    CheckinStatus.CONNECTION_ERROR,
    CheckinStatus.PAGE_LOAD_FAILED,
    CheckinStatus.SCHOOL_API_ERROR,
    CheckinStatus.TOKEN_INVALID,
}
TERMINAL_SUCCESS_STATUSES = {
    CheckinStatus.NO_TASK,
    CheckinStatus.SUCCESS,
    CheckinStatus.ALREADY_CHECKED_IN,
    CheckinStatus.ON_LEAVE,
}


def _resolve_accounts(cli_username=None, cli_password=None):
    return config.resolve_accounts(
        cli_username,
        cli_password,
        config_dir=CONFIG_DIR,
    )


def check_in(username: str, password: str, timeout: int = 10, force_login: bool = False, deadline=None):
    """Run one account through the lazily imported runtime service."""

    from .checkin_service import check_in as service_check_in

    return service_check_in(
        username,
        password,
        timeout=timeout,
        force_login=force_login,
        deadline=deadline,
    )


def check_dependency(name, import_name=None):
    """Check installation metadata without importing runtime modules."""

    try:
        module_name = import_name or name
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            return False, f"找不到模块 {module_name}"
        return True, None
    except Exception as exc:
        return False, str(exc)


def run_config_check(cli_username=None, cli_password=None, *, config_dir=None):
    """Print a local configuration report; never access the school network."""

    directory = config.get_config_dir(CONFIG_DIR if config_dir is None else config_dir)
    print("SWU 自动打卡配置检查")
    print("=" * 28)
    print(f"配置目录：{directory}")

    resolution = config.resolve_accounts(
        cli_username,
        cli_password,
        config_dir=directory,
    )
    accounts = resolution.accounts
    errors = list(resolution.errors)
    if accounts:
        print(f"[OK] 账号配置：{resolution.source}，共 {len(accounts)} 个账号")
        for idx, account in enumerate(accounts, 1):
            print(f"     {idx}. {config.mask_account(account['username'])}")
    else:
        print("[FAIL] 账号配置：未找到可用账号")
        print("       请配置 users.json、SWU_USERS，或 SWU_USERNAME/SWU_PASSWORD。")

    token_cache_path = os.path.join(directory, ".token_cache.json")
    if os.path.exists(token_cache_path):
        try:
            with open(token_cache_path, encoding="utf-8") as handle:
                cached_tokens = json.load(handle)
            print(f"[OK] Token 缓存：已存在，包含 {len(cached_tokens)} 个账号")
        except Exception as exc:
            print(f"[WARN] Token 缓存：文件存在但无法读取，后续会自动重新登录 ({exc})")
    else:
        print("[INFO] Token 缓存：未发现，首次运行会按登录方式获取 Token")

    channels = config.configured_push_channels()
    push_errors = config.push_configuration_errors()
    if channels:
        print(f"[OK] 推送配置：已配置 {', '.join(channels)}")
    if push_errors:
        for push_error in push_errors:
            print(f"[FAIL] 推送配置：{push_error}")
    if not channels and not push_errors:
        print("[INFO] 推送配置：未配置，运行结束后只输出日志")
    errors.extend(push_errors)

    dependencies = (
        ("requests", "requests"),
        ("playwright", "playwright.sync_api"),
        ("ddddocr", "ddddocr"),
        ("python-dotenv", "dotenv"),
    )
    dependencies_ok = True
    for label, import_name in dependencies:
        ok, error = check_dependency(label, import_name)
        if ok:
            print(f"[OK] 依赖：{label}")
        else:
            dependencies_ok = False
            print(f"[FAIL] 依赖：{label} 未安装或不可用 ({error})")

    runtime_options = config.parse_runtime_options()
    runtime_issues = {issue.spec.key: issue for issue in config.runtime_parameter_issues()}
    print("运行参数：")
    for spec in config.RUNTIME_PARAMETER_SPECS:
        value = getattr(runtime_options, spec.key)
        issue = runtime_issues.get(spec.key)
        if issue is not None:
            prefix = "并发配置：" if spec.key == "max_workers" else "运行参数："
            print(
                f"[WARN] {prefix}{spec.env_name}={issue.raw_value!r} 无效（允许 "
                f"{spec.minimum}-{spec.maximum}），将使用默认值 {value}"
            )
        elif spec.key == "max_workers":
            print(f"[OK] 并发配置：最大线程数 {value} （{spec.env_name}={value}，允许 {spec.minimum}-{spec.maximum}）")
        else:
            print(f"[OK] {spec.label}：{spec.env_name}={value} （允许 {spec.minimum}-{spec.maximum}）")

    if errors:
        print("\n需要处理的问题：")
        for error in errors:
            print(f"- {error}")

    if accounts and dependencies_ok and not errors:
        print("\n配置检查通过。")
        return 0
    print("\n配置检查未通过，请先处理上面的 FAIL 项。")
    return 1


def run_network_check(timeout=5):
    """Run the opt-in school connectivity probe and return a process status."""

    try:
        from .api.school import check_school_connectivity

        connected, message = check_school_connectivity(timeout=timeout)
    except Exception as exc:
        connected, message = False, f"网络诊断依赖不可用：{exc}"
    if connected:
        print(f"[OK] 学校官网连通性：{message}")
        return 0
    print(f"[FAIL] 学校官网连通性：{message}")
    return 1


def run_menu():
    """Open the menu without importing the school runtime."""

    from . import menu

    config.load_dotenv_file(CONFIG_DIR)
    return menu.run_menu(
        CONFIG_DIR,
        config_check=run_config_check,
        script_path=__file__,
    )


_RUN_LOCK_FD = None
_RUN_LOCK_STYLE = None


def acquire_run_lock():
    """Acquire an OS lock; never infer liveness from a file age."""
    global _RUN_LOCK_FD, _RUN_LOCK_STYLE
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    lock_path = os.path.join(CONFIG_DIR, ".run.lock")
    open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, open_flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                logger.warning("检测到已有任务正在运行，跳过本次执行。锁文件：%s", lock_path)
                return None
            _RUN_LOCK_STYLE = "msvcrt"
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if getattr(exc, "errno", None) not in (errno.EACCES, errno.EAGAIN):
                    raise
                os.close(fd)
                logger.warning("检测到已有任务正在运行，跳过本次执行。锁文件：%s", lock_path)
                return None
            _RUN_LOCK_STYLE = "fcntl"

        metadata = (
            json.dumps(
                {"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False,
            )
            + "\n"
        )
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(fd, 0o600)
            except (AttributeError, NotImplementedError, OSError):
                os.chmod(lock_path, 0o600)
        else:
            os.chmod(lock_path, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, metadata.encode("utf-8"))
        os.fsync(fd)
        _RUN_LOCK_FD = fd
        return lock_path
    except Exception:
        with contextlib.suppress(Exception):
            if _RUN_LOCK_STYLE == "msvcrt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif _RUN_LOCK_STYLE == "fcntl":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        _RUN_LOCK_STYLE = None
        raise


def release_run_lock(lock_path):
    global _RUN_LOCK_FD, _RUN_LOCK_STYLE
    if _RUN_LOCK_FD is None:
        return
    try:
        if _RUN_LOCK_STYLE == "msvcrt":
            import msvcrt

            os.lseek(_RUN_LOCK_FD, 0, os.SEEK_SET)
            msvcrt.locking(_RUN_LOCK_FD, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_RUN_LOCK_FD, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("释放运行锁失败：%s", exc)
    finally:
        with contextlib.suppress(OSError):
            os.close(_RUN_LOCK_FD)
        _RUN_LOCK_FD = None
        _RUN_LOCK_STYLE = None


def run_accounts(accounts, force_login=False, checkin_func=None, sleep_func=time.sleep, clock=time.monotonic):
    from .runner import run_accounts as _run_accounts

    return _run_accounts(
        accounts,
        force_login=force_login,
        checkin_func=checkin_func or check_in,
        validate_accounts=config.validate_accounts,
        status_messages=STATUS_MESSAGES,
        retryable_statuses=RETRYABLE_STATUSES,
        terminal_success_statuses=TERMINAL_SUCCESS_STATUSES,
        deadline_exception=TimeoutError,
        sleep_func=sleep_func,
        clock=clock,
        logger=logger,
    )


def build_parser():
    """Build the CLI parser without importing the school runtime."""

    import argparse

    parser = argparse.ArgumentParser(description="西南大学自动打卡脚本")
    parser.add_argument(
        "-u",
        "--username",
        type=str,
        help="临时的校园网账号（若配置，将忽略 users.json 和环境变量）",
    )
    parser.add_argument("-p", "--password", type=str, help="临时的校园网密码")
    parser.add_argument(
        "-f",
        "--force-login",
        action="store_true",
        help="强制通过浏览器重新登录（忽略 Token 缓存）",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="检查账号、依赖、推送和缓存配置，不访问学校网络",
    )
    parser.add_argument(
        "--check-network",
        action="store_true",
        help="手动诊断学校官网连通性（会访问学校网络）",
    )
    parser.add_argument("-m", "--menu", action="store_true", help="打开数字配置菜单")
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="跳过运行锁检查，通常不建议在定时任务中使用",
    )
    return parser


def _configure_basic_logging() -> None:
    level_name = os.getenv("SWU_LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    """Run the selected CLI action and return its process status."""

    args = build_parser().parse_args(argv)
    if args.menu:
        return run_menu()
    if args.check_network:
        return run_network_check()
    if args.check_config:
        return run_config_check(args.username, args.password)

    _configure_basic_logging()
    resolution = _resolve_accounts(args.username, args.password)
    if resolution.errors:
        for error in resolution.errors:
            logger.error("账号配置错误：%s", error)
        return 1

    accounts = resolution.accounts
    if accounts:
        logger.info("已从%s读取并校验通过 %s 个账号信息。", resolution.source, len(accounts))
    else:
        try:
            from . import menu

            accounts = menu.create_accounts_wizard(CONFIG_DIR)
        except (KeyboardInterrupt, EOFError):
            print("\n已取消配置向导。")
            return 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.error("写入账号配置失败：%s", exc)
            return 1

    if not accounts:
        logger.error("未配置账号信息！请提供 users.json、SWU_USERS，或 SWU_USERNAME/SWU_PASSWORD。")
        return 1

    try:
        from . import checkin_service
        from .logging_utils import setup_logging
    except ImportError as exc:
        logger.error("签到运行依赖未安装或不可用：%s", exc)
        logger.error("请先安装 requirements.txt 中的依赖；--check-config 可离线检查。")
        return 1

    setup_logging()
    run_lock_path = None
    if not args.no_lock:
        try:
            run_lock_path = acquire_run_lock()
        except Exception as exc:
            logger.error("无法建立跨进程运行锁，停止执行以避免重复签到：%s", exc)
            return 1
        if run_lock_path is None:
            return 0
        atexit.register(release_run_lock, run_lock_path)

    logger.info("开始执行签到...")
    try:
        summary_title = "西南大学自动签到任务通知"
        summary_content, exit_code, _ = run_accounts(
            accounts,
            force_login=args.force_login,
            checkin_func=checkin_service.check_in,
        )
        try:
            from .notify import send_push

            send_push(summary_title, summary_content)
        except Exception as exc:
            logger.error("发送消息推送异常: %s", exc)
        return exit_code
    finally:
        if run_lock_path is not None:
            atexit.unregister(release_run_lock)
            release_run_lock(run_lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
