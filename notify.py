import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager

import requests

import config

logger = logging.getLogger("swu.notify")

REQUEST_TIMEOUT_SECONDS = 10
_PUSH_DEADLINE_SPEC = config.RUNTIME_PARAMETER_BY_KEY["push_deadline_seconds"]
DEFAULT_PUSH_DEADLINE_SECONDS = _PUSH_DEADLINE_SPEC.default
MIN_PUSH_DEADLINE_SECONDS = _PUSH_DEADLINE_SPEC.minimum
MAX_PUSH_DEADLINE_SECONDS = _PUSH_DEADLINE_SPEC.maximum
PUSH_DEADLINE_ENV = _PUSH_DEADLINE_SPEC.env_name
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_MAX_RETRIES = 2
TELEGRAM_MAX_RETRY_AFTER_SECONDS = 30


class _DropHttpDebugRecords(logging.Filter):
    def filter(self, _record):
        return False


@contextmanager
def _suppress_http_debug_logging():
    """Hide third-party HTTP records only while a push request is in flight."""
    logger_names = (
        "requests",
        "requests.api",
        "requests.adapters",
        "requests.sessions",
        "requests.packages.urllib3",
        "requests.packages.urllib3.connection",
        "requests.packages.urllib3.connectionpool",
        "urllib3",
        "urllib3.connection",
        "urllib3.connectionpool",
    )
    installed = []
    try:
        for name in logger_names:
            target = logging.getLogger(name)
            filter_ = _DropHttpDebugRecords()
            target.addFilter(filter_)
            installed.append((target, filter_))
        yield
    finally:
        for target, filter_ in installed:
            target.removeFilter(filter_)


def _configure_direct_session(session):
    """Make a requests session ignore ambient proxy configuration."""
    try:
        session.trust_env = False
    except Exception:
        # Keep lightweight test doubles and compatible session wrappers usable.
        pass
    # ``trust_env`` prevents requests from reading proxy environment variables.
    # Clearing this mapping also protects callers that supplied a preconfigured
    # session and makes the direct-connection intent explicit.
    try:
        session.proxies = {}
    except Exception:
        # Small test doubles do not necessarily expose ``proxies``.
        pass
    return session


def _new_direct_session():
    return _configure_direct_session(requests.Session())


def _effective_clock(clock):
    return time.monotonic if clock is None else clock


def _effective_sleep(sleep_func):
    return time.sleep if sleep_func is None else sleep_func


def _remaining_seconds(deadline, clock=None):
    """Return non-negative seconds left in a cooperative push deadline."""
    if deadline is None:
        return None
    current = _effective_clock(clock)()
    return max(0.0, float(deadline) - float(current))


def _request_timeout(deadline=None, clock=None):
    """Bound one requests timeout by the remaining cooperative deadline."""
    if deadline is None:
        return REQUEST_TIMEOUT_SECONDS
    remaining = _remaining_seconds(deadline, clock)
    if remaining <= 0:
        return None
    return min(float(REQUEST_TIMEOUT_SECONDS), remaining)


def _configured_push_deadline_seconds(logger_=None):
    """Read the shared bounded push budget from the central parser."""

    return config.parse_runtime_options(logger=logger_).push_deadline_seconds


def _call_with_session(session, operation, channel_name, *, deadline=None, clock=None):
    """Run one channel operation and close a session created for that call."""
    if deadline is not None and _remaining_seconds(deadline, clock) <= 0:
        return False
    client = session
    owns_session = session is None
    try:
        if client is None:
            client = _new_direct_session()
        else:
            _configure_direct_session(client)
        return bool(operation(client))
    except Exception:
        # Request exceptions can contain the complete URL.  Keep logs generic so
        # bot tokens, endpoint URLs, and request payloads never reach the log.
        logger.error("%s推送失败", channel_name)
        return False
    finally:
        if owns_session and client is not None:
            try:
                client.close()
            except Exception:
                # A close failure must not hide the channel result or leak data.
                pass


def _status_code(response):
    try:
        return int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        return None


def _json_object(response):
    try:
        value = response.json()
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _http_ok(response):
    status = _status_code(response)
    return status is not None and 200 <= status < 300


def _send_json_channel(session, url, payload, success_key, channel_name, *, deadline=None, clock=None):
    timeout = _request_timeout(deadline, clock)
    if timeout is None:
        return False
    with _suppress_http_debug_logging():
        response = session.post(
            url,
            json=payload,
            timeout=timeout,
        )
    if not _http_ok(response):
        return False
    body = _json_object(response)
    return body is not None and body.get(success_key) == 0


def send_dingtalk(token, secret, title, content, session=None, *, deadline=None, clock=None):
    def operation(client):
        url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
        if secret:
            timestamp = str(round(time.time() * 1000))
            secret_enc = secret.encode("utf-8")
            string_to_sign = f"{timestamp}\n{secret}"
            string_to_sign_enc = string_to_sign.encode("utf-8")
            hmac_code = hmac.new(
                secret_enc,
                string_to_sign_enc,
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url += f"&timestamp={timestamp}&sign={sign}"

        payload = {
            "msgtype": "text",
            "text": {"content": f"{title}\n\n{content}"},
        }
        return _send_json_channel(
            client,
            url,
            payload,
            "errcode",
            "钉钉机器人",
            deadline=deadline,
            clock=clock,
        )

    return _call_with_session(session, operation, "钉钉机器人", deadline=deadline, clock=clock)


def send_qywx(key, title, content, session=None, *, deadline=None, clock=None):
    def operation(client):
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        payload = {
            "msgtype": "text",
            "text": {"content": f"{title}\n\n{content}"},
        }
        return _send_json_channel(
            client,
            url,
            payload,
            "errcode",
            "企业微信群机器人",
            deadline=deadline,
            clock=clock,
        )

    return _call_with_session(session, operation, "企业微信群机器人", deadline=deadline, clock=clock)


def send_bark(key, url, title, content, session=None, *, deadline=None, clock=None):
    def operation(client):
        base_url = url.rstrip("/") if url else "https://api.day.app"
        request_url = f"{base_url}/{key}/{urllib.parse.quote(str(title))}/{urllib.parse.quote(str(content))}"
        timeout = _request_timeout(deadline, clock)
        if timeout is None:
            return False
        with _suppress_http_debug_logging():
            response = client.get(request_url, timeout=timeout)
        return _http_ok(response)

    return _call_with_session(session, operation, "Bark", deadline=deadline, clock=clock)


def send_serverchan(key, title, content, session=None, *, deadline=None, clock=None):
    def operation(client):
        url = f"https://sctapi.ftqq.com/{key}.send"
        payload = {"title": title, "desp": content}
        return _send_form_channel(
            client,
            url,
            payload,
            "Server酱",
            deadline=deadline,
            clock=clock,
        )

    return _call_with_session(session, operation, "Server酱", deadline=deadline, clock=clock)


def send_pushdeer(key, title, content, session=None, *, deadline=None, clock=None):
    def operation(client):
        url = "https://api2.pushdeer.com/message/push"
        payload = {"pushkey": key, "text": title, "desp": content}
        return _send_form_channel(
            client,
            url,
            payload,
            "PushDeer",
            deadline=deadline,
            clock=clock,
        )

    return _call_with_session(session, operation, "PushDeer", deadline=deadline, clock=clock)


def _send_form_channel(session, url, payload, channel_name, *, deadline=None, clock=None):
    timeout = _request_timeout(deadline, clock)
    if timeout is None:
        return False
    with _suppress_http_debug_logging():
        response = session.post(
            url,
            data=payload,
            timeout=timeout,
        )
    if not _http_ok(response):
        return False
    body = _json_object(response)
    return body is not None and body.get("code") == 0


def _split_telegram_message(text, limit=TELEGRAM_MAX_MESSAGE_LENGTH):
    """Split by Python Unicode code points without cutting UTF-8 characters."""
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]


def _retry_after_seconds(body):
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    if isinstance(value, bool):
        return None
    try:
        # ``value`` comes from untrusted JSON; ``str`` keeps the conversion
        # total so non-numeric payloads raise inside the guard below.
        delay = float(str(value))
    except (TypeError, ValueError):
        return None
    if delay != delay or delay in (float("inf"), float("-inf")):
        return None
    if delay < 0 or delay > TELEGRAM_MAX_RETRY_AFTER_SECONDS:
        return None
    return delay


def _send_telegram_chunk(
    session,
    url,
    chat_id,
    text,
    *,
    deadline=None,
    clock=None,
    sleep_func=None,
):
    payload = {"chat_id": chat_id, "text": text}
    retries = 0
    sleeper = _effective_sleep(sleep_func)

    while True:
        timeout = _request_timeout(deadline, clock)
        if timeout is None:
            return False
        # A write timeout leaves delivery ambiguous.  It is deliberately caught
        # by the caller and never retried, avoiding duplicate Telegram messages.
        with _suppress_http_debug_logging():
            response = session.post(
                url,
                json=payload,
                timeout=timeout,
            )
        status = _status_code(response)
        body = _json_object(response)

        if _http_ok(response) and body is not None and body.get("ok") is True:
            return True

        is_rate_limited = status == 429 or (isinstance(body, dict) and body.get("error_code") == 429)
        if not is_rate_limited or retries >= TELEGRAM_MAX_RETRIES:
            return False

        delay = _retry_after_seconds(body)
        if delay is None:
            return False

        # Waiting the full retry interval is required by Telegram.  Never sleep
        # a truncated interval and retry early after the cooperative deadline.
        if deadline is not None:
            remaining = _remaining_seconds(deadline, clock)
            if delay >= remaining:
                return False
        sleeper(delay)
        retries += 1


def send_telegram(
    bot_token,
    chat_id,
    title,
    content,
    session=None,
    *,
    deadline=None,
    clock=None,
    sleep_func=None,
):
    """Send plain-text Telegram messages using the official Bot API."""
    token = "" if bot_token is None else str(bot_token).strip()
    normalized_chat_id = chat_id.strip() if isinstance(chat_id, str) else chat_id
    if not token or normalized_chat_id is None or (isinstance(normalized_chat_id, str) and not normalized_chat_id):
        logger.warning(
            "Telegram 推送配置不完整：需同时设置 PUSH_TELEGRAM_BOT_TOKEN 和 PUSH_TELEGRAM_CHAT_ID，跳过请求。"
        )
        return False

    message = f"{title}\n\n{content}"
    chunks = _split_telegram_message(message)

    def operation(client):
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chunk in chunks:
            if not _send_telegram_chunk(
                client,
                url,
                normalized_chat_id,
                chunk,
                deadline=deadline,
                clock=clock,
                sleep_func=sleep_func,
            ):
                return False
        return True

    return _call_with_session(session, operation, "Telegram", deadline=deadline, clock=clock)


def _env_value(name):
    return os.getenv(name, "").strip()


_PUSH_SENDERS: dict[str, Callable[..., bool]] = {
    "DingTalk": send_dingtalk,
    "WeChat Work": send_qywx,
    "Bark": send_bark,
    "ServerChan": send_serverchan,
    "PushDeer": send_pushdeer,
    "Telegram": send_telegram,
}


def _channel_arguments(channel, title, content):
    """Build sender arguments from one shared channel registration row."""

    env_names = channel.required_env + channel.optional_env
    values = tuple(_env_value(name) for name in env_names)
    # Every sender takes its table values followed by the common title/body.
    # This keeps registration data in config.py and avoids another credential
    # list in this module.
    return (*values, title, content)


def send_push(title, content, session=None, *, clock=None, sleep_func=None):
    """Send through every configured channel and return whether one succeeded."""
    effective_clock = _effective_clock(clock)
    effective_sleep = _effective_sleep(sleep_func)
    push_deadline = effective_clock() + _configured_push_deadline_seconds(logger)
    deadline = float(push_deadline)

    channels = []
    for channel in config.PUSH_CHANNELS:
        present = [_env_value(name) for name in channel.required_env]
        if not any(present):
            continue
        if not all(present):
            if channel.name == "Telegram":
                logger.warning(
                    "Telegram 推送配置不完整：需同时设置 PUSH_TELEGRAM_BOT_TOKEN 和 PUSH_TELEGRAM_CHAT_ID，跳过请求。"
                )
            else:
                missing = [name for name, value in zip(channel.required_env, present, strict=True) if not value]
                logger.warning(
                    "%s 推送配置不完整，缺少 %s，跳过请求。",
                    channel.menu_label,
                    ", ".join(missing),
                )
            continue
        sender = _PUSH_SENDERS.get(channel.name)
        if sender is None:
            logger.warning("未找到%s推送实现，跳过请求。", channel.menu_label)
            continue
        channels.append(
            (
                channel.menu_label,
                sender,
                _channel_arguments(channel, title, content),
            )
        )

    if not channels:
        logger.info("未配置任何推送通道 (如 PUSH_DINGTALK_TOKEN, PUSH_BARK_KEY 等)，跳过推送。")
        return False

    client = session
    owns_session = session is None
    sent = False
    try:
        if client is None:
            client = _new_direct_session()
        else:
            _configure_direct_session(client)

        for channel_name, sender, args in channels:
            remaining = _remaining_seconds(deadline, effective_clock)
            if remaining <= 0:
                logger.warning("推送总预算已耗尽，跳过剩余通道。")
                break
            logger.info("正在发送%s推送...", channel_name)
            try:
                sender_kwargs = {
                    "session": client,
                    "deadline": deadline,
                    "clock": effective_clock,
                }
                if sender is send_telegram:
                    sender_kwargs["sleep_func"] = effective_sleep
                succeeded = bool(sender(*args, **sender_kwargs))
            except Exception:
                # A broken channel must not prevent the remaining channels.
                succeeded = False
            if succeeded:
                logger.info("%s推送成功", channel_name)
                sent = True
            else:
                logger.error("%s推送失败", channel_name)
    except Exception:
        logger.error("推送会话初始化失败")
    finally:
        if owns_session and client is not None:
            try:
                client.close()
            except Exception:
                pass

    if not sent:
        logger.warning("推送配置存在，但发送失败。")
    return sent
