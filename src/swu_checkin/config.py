"""Local configuration helpers for the SWU check-in command.

This module deliberately has no browser or HTTP imports.  It owns account
validation, dotenv loading, and the small file operations used by the menu so
that ``check_in.py --help`` and configuration tools remain usable before the
runtime dependencies are installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .atomic_io import atomic_write_text


def _default_base_dir() -> str:
    """Return the directory that holds ``users.json`` and ``.env``.

    The historical flat layout resolved this to the repository root because
    every module lived there.  The package now lives in ``src/swu_checkin``,
    so the same directory is derived from the ``src`` layout instead.  Wheel
    installs land in ``site-packages``, which is not writable user space, and
    fall back to the working directory; deployments should set
    ``SWU_CONFIG_DIR`` explicitly (the Docker image uses ``/data``).
    """

    package_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(package_dir)
    if os.path.basename(parent_dir) == "src":
        return os.path.dirname(parent_dir)
    if os.path.basename(parent_dir) in {"site-packages", "dist-packages"}:
        return os.getcwd()
    return package_dir


BASE_DIR = _default_base_dir()
CONFIG_DIR = os.path.abspath(os.getenv("SWU_CONFIG_DIR", BASE_DIR))

logger = logging.getLogger("swu.config")

_ENV_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class RuntimeParameterSpec:
    """Definition for one bounded runtime setting.

    Keeping the environment name, range, and default together prevents the
    runtime and the offline configuration report from drifting apart.
    """

    key: str
    env_name: str
    default: int
    minimum: int
    maximum: int
    label: str


@dataclass(frozen=True)
class RuntimeOptions:
    """Effective values for all bounded runtime settings."""

    max_workers: int
    max_rounds: int
    retry_interval_seconds: int
    run_deadline_seconds: int
    push_deadline_seconds: int


@dataclass(frozen=True)
class RuntimeParameterIssue:
    """A malformed runtime value and the setting it belongs to."""

    spec: RuntimeParameterSpec
    raw_value: str


RUNTIME_PARAMETER_SPECS = (
    RuntimeParameterSpec(
        "max_workers",
        "SWU_MAX_WORKERS",
        default=3,
        minimum=1,
        maximum=32,
        label="最大线程数",
    ),
    RuntimeParameterSpec(
        "max_rounds",
        "SWU_MAX_ROUNDS",
        default=3,
        minimum=1,
        maximum=20,
        label="失败账号最多重试轮数",
    ),
    RuntimeParameterSpec(
        "retry_interval_seconds",
        "SWU_RETRY_INTERVAL_SECONDS",
        default=300,
        minimum=1,
        maximum=3600,
        label="失败账号重试间隔（秒）",
    ),
    RuntimeParameterSpec(
        "run_deadline_seconds",
        "SWU_RUN_DEADLINE_SECONDS",
        default=900,
        minimum=1,
        maximum=3600,
        label="单次任务总时限（秒）",
    ),
    RuntimeParameterSpec(
        "push_deadline_seconds",
        "SWU_PUSH_DEADLINE_SECONDS",
        default=60,
        minimum=1,
        maximum=3600,
        label="推送共享总预算（秒）",
    ),
)
RUNTIME_PARAMETER_BY_KEY = {spec.key: spec for spec in RUNTIME_PARAMETER_SPECS}


@dataclass(frozen=True)
class PushChannelSpec:
    """Small shared registration row for one push channel."""

    name: str
    menu_label: str
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...] = ()


PUSH_CHANNELS = (
    PushChannelSpec(
        "DingTalk",
        "钉钉机器人",
        ("PUSH_DINGTALK_TOKEN",),
        ("PUSH_DINGTALK_SECRET",),
    ),
    PushChannelSpec("WeChat Work", "企业微信群机器人", ("PUSH_QYWX_KEY",)),
    PushChannelSpec(
        "Bark",
        "Bark",
        ("PUSH_BARK_KEY",),
        ("PUSH_BARK_URL",),
    ),
    PushChannelSpec("ServerChan", "Server 酱", ("PUSH_SERVERCHAN_KEY",)),
    PushChannelSpec("PushDeer", "PushDeer", ("PUSH_PUSHDEER_KEY",)),
    PushChannelSpec(
        "Telegram",
        "Telegram",
        ("PUSH_TELEGRAM_BOT_TOKEN", "PUSH_TELEGRAM_CHAT_ID"),
    ),
)


@dataclass(frozen=True)
class AccountResolution:
    """Result of resolving one account source in priority order."""

    accounts: list[dict[str, str]]
    source: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.accounts) and not self.errors


def get_config_dir(config_dir: str | os.PathLike[str] | None = None) -> str:
    """Return an absolute configuration directory.

    The optional argument makes callers and tests explicit about which
    directory they are operating on while preserving the historical module
    level ``CONFIG_DIR`` override used by the menu.
    """

    return os.path.abspath(os.fspath(CONFIG_DIR if config_dir is None else config_dir))


def users_config_path(config_dir: str | os.PathLike[str] | None = None) -> str:
    return os.path.join(get_config_dir(config_dir), "users.json")


def env_config_path(config_dir: str | os.PathLike[str] | None = None) -> str:
    return os.path.join(get_config_dir(config_dir), ".env")


def _read_runtime_options(
    environ: Mapping[str, str] | None = None,
) -> tuple[RuntimeOptions, tuple[RuntimeParameterIssue, ...]]:
    """Parse all bounded runtime values from one environment mapping."""

    env = os.environ if environ is None else environ
    values: dict[str, int] = {}
    issues: list[RuntimeParameterIssue] = []
    for spec in RUNTIME_PARAMETER_SPECS:
        raw = env.get(spec.env_name, "")
        raw_text = "" if raw is None else str(raw).strip()
        value = spec.default
        if raw_text:
            try:
                candidate = int(raw_text, 10)
            except (TypeError, ValueError):
                issues.append(RuntimeParameterIssue(spec, raw_text))
            else:
                if spec.minimum <= candidate <= spec.maximum:
                    value = candidate
                else:
                    issues.append(RuntimeParameterIssue(spec, raw_text))
        values[spec.key] = value
    return RuntimeOptions(**values), tuple(issues)


def parse_runtime_options(
    environ: Mapping[str, str] | None = None,
    *,
    logger=None,
) -> RuntimeOptions:
    """Return effective runtime settings using one bounded parser.

    Invalid non-empty values fall back to the per-setting default.  The
    optional logger is used by actual runs; callers such as ``--check-config``
    can inspect :func:`runtime_parameter_issues` to render the same result
    without logging.
    """

    options, issues = _read_runtime_options(environ)
    if logger is not None:
        for issue in issues:
            spec = issue.spec
            logger.warning(
                "%s=%s 无效（允许 %s-%s），将使用默认值 %s。",
                spec.env_name,
                issue.raw_value,
                spec.minimum,
                spec.maximum,
                spec.default,
            )
    return options


def runtime_parameter_issues(
    environ: Mapping[str, str] | None = None,
) -> tuple[RuntimeParameterIssue, ...]:
    """Return malformed runtime values without emitting logs."""

    _options, issues = _read_runtime_options(environ)
    return issues


def load_dotenv_file(config_dir: str | os.PathLike[str] | None = None) -> bool:
    """Load the selected ``.env`` before reading any account source.

    ``override=False`` preserves values supplied by the process environment,
    which is required for CI secrets to take precedence over a local file.
    Importing python-dotenv is intentionally deferred: help, menus, and source
    validation can still start in a minimal Python environment.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(
        load_dotenv(
            env_config_path(config_dir),
            override=False,
            interpolate=False,
        )
    )


def _atomic_write_text(path: str, content: str, mode: int = 0o600) -> None:
    """Write a sensitive configuration file without exposing partial data."""

    atomic_write_text(path, content, mode=mode, prefix=".swu-write-")


def validate_accounts(accounts) -> list[dict[str, str]]:
    """Validate, normalize, and de-duplicate an account list.

    Usernames are stripped for matching; passwords are opaque and keep their
    leading/trailing spaces exactly as supplied.
    """

    if not isinstance(accounts, list):
        raise ValueError("账号配置必须是 JSON 数组格式（List）")

    validated: list[dict[str, str]] = []
    seen_usernames: set[str] = set()
    for idx, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            raise ValueError(f"第 {idx} 个账号配置格式错误：应为 JSON 对象（键值对）")

        username = account.get("username")
        password = account.get("password")
        if username is None or password is None:
            raise ValueError(f"第 {idx} 个账号配置不完整：必须包含 'username' 和 'password' 字段")
        if not isinstance(username, (str, int, float)) or isinstance(username, bool):
            raise ValueError(f"第 {idx} 个账号配置类型错误：'username' 必须是字符串或数字类型")
        if not isinstance(password, (str, int, float)) or isinstance(password, bool):
            raise ValueError(f"第 {idx} 个账号配置类型错误：'password' 必须是字符串或数字类型")

        username_str = str(username).strip()
        password_str = str(password)
        if not username_str:
            raise ValueError(f"第 {idx} 个账号配置错误：'username' 不能为空")
        if password_str == "":
            raise ValueError(f"第 {idx} 个账号配置错误：'password' 不能为空")

        if username_str in seen_usernames:
            logger.warning("第 %s 个账号与前面的账号重复，已忽略重复配置。", idx)
            continue
        seen_usernames.add(username_str)
        validated.append({"username": username_str, "password": password_str})
    return validated


def load_users_file(config_dir: str | os.PathLike[str] | None = None) -> list[dict[str, str]]:
    path = users_config_path(config_dir)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return validate_accounts(json.load(handle))


def save_users_file(accounts, config_dir: str | os.PathLike[str] | None = None) -> None:
    validated = validate_accounts(accounts)
    directory = get_config_dir(config_dir)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _atomic_write_text(
        users_config_path(directory),
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
    )


def _dotenv_quote(value) -> str:
    """Encode a value so python-dotenv reads it back literally."""

    value = str(value)
    if "\r" in value or "\n" in value:
        raise ValueError(".env 值不能包含换行符")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def set_env_value(
    key: str,
    value,
    config_dir: str | os.PathLike[str] | None = None,
) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
        raise ValueError(f"无效的环境变量名称：{key!r}")
    key = str(key)
    value = str(value)
    path = env_config_path(config_dir)
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    encoded_value = _dotenv_quote(value)
    updated: list[str] = []
    found = False
    for line in lines:
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match and match.group(1) == key:
            updated.append(f"{key}={encoded_value}")
            found = True
        else:
            updated.append(line)
    if not found:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={encoded_value}")

    _atomic_write_text(path, "\n".join(updated).rstrip("\n") + "\n")
    os.environ[key] = value


def unset_env_value(*keys: str, config_dir: str | os.PathLike[str] | None = None) -> None:
    for key in keys:
        os.environ.pop(key, None)
    path = env_config_path(config_dir)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    key_set = set(keys)

    def _key_of(line: str) -> str | None:
        match = _ENV_ASSIGNMENT_RE.match(line)
        return match.group(1) if match else None

    updated = [line for line in lines if _key_of(line) not in key_set]
    _atomic_write_text(path, "\n".join(updated).rstrip("\n") + "\n")


def mask_account(value: str) -> str:
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"


def _env_value_present(environ: Mapping[str, str], name: str) -> bool:
    value = environ.get(name, "")
    return value is not None and bool(str(value).strip())


def configured_push_channels(environ: Mapping[str, str] | None = None) -> list[str]:
    """List push channels with complete required credentials."""

    env = os.environ if environ is None else environ
    return [
        channel.name for channel in PUSH_CHANNELS if all(_env_value_present(env, name) for name in channel.required_env)
    ]


def push_configuration_errors(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return actionable errors for channels with incomplete settings."""

    env = os.environ if environ is None else environ
    errors: list[str] = []
    for channel in PUSH_CHANNELS:
        present = [name for name in channel.required_env if _env_value_present(env, name)]
        if present and len(present) < len(channel.required_env):
            missing = [name for name in channel.required_env if name not in present]
            errors.append(f"{channel.name} 推送缺少 {', '.join(missing)}。")
    return errors


def _source_error(source: str, exc: Exception) -> str:
    if source == "users.json":
        if isinstance(exc, json.JSONDecodeError):
            return f"users.json 不是合法 JSON：第 {exc.lineno} 行，第 {exc.colno} 列，{exc.msg}"
        return f"users.json 读取或校验失败：{exc}"
    if source == "环境变量 SWU_USERS":
        if isinstance(exc, json.JSONDecodeError):
            return f"环境变量 SWU_USERS 不是合法 JSON：{exc}"
        return f"环境变量 SWU_USERS 无效：{exc}"
    if source == "命令行参数":
        return f"命令行账号参数无效：{exc}"
    return f"单账号环境变量无效：{exc}"


def resolve_accounts(
    cli_username=None,
    cli_password=None,
    *,
    config_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AccountResolution:
    """Resolve accounts in the documented priority order.

    A configured, non-empty source that is malformed is an error and stops
    fallback.  Empty lists intentionally fall through to the next source, as
    the original command did.
    """

    load_dotenv_file(config_dir)
    env = os.environ if environ is None else environ

    cli_present = cli_username is not None or cli_password is not None
    if cli_present:
        if cli_username is None or cli_password is None:
            return AccountResolution(
                [],
                "命令行参数",
                ("命令行账号参数不完整：-u/--username 和 -p/--password 必须同时提供。",),
            )
        try:
            accounts = validate_accounts([{"username": cli_username, "password": cli_password}])
        except ValueError as exc:
            return AccountResolution([], "命令行参数", (_source_error("命令行参数", exc),))
        return AccountResolution(accounts, "命令行参数")

    users_path = users_config_path(config_dir)
    if os.path.exists(users_path):
        try:
            accounts = load_users_file(config_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return AccountResolution([], "users.json", (_source_error("users.json", exc),))
        if accounts:
            return AccountResolution(accounts, "users.json")

    swu_users_raw = str(env.get("SWU_USERS", "")).strip()
    if swu_users_raw:
        try:
            accounts = validate_accounts(json.loads(swu_users_raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return AccountResolution([], "环境变量 SWU_USERS", (_source_error("环境变量 SWU_USERS", exc),))
        if accounts:
            return AccountResolution(accounts, "环境变量 SWU_USERS")

    username = str(env.get("SWU_USERNAME", "")).strip()
    password = env.get("SWU_PASSWORD", "")
    if username or password:
        try:
            accounts = validate_accounts([{"username": username, "password": password}])
        except (ValueError, TypeError) as exc:
            return AccountResolution([], "环境变量 SWU_USERNAME/SWU_PASSWORD", (_source_error("单账号环境变量", exc),))
        if accounts:
            return AccountResolution(accounts, "环境变量 SWU_USERNAME/SWU_PASSWORD")

    return AccountResolution([])


# ``load_accounts`` is a descriptive alias for callers that do not need to
# know the implementation's historical name.
load_accounts = resolve_accounts
