"""Token 提取、交换、校验与缓存。

登录成功后 Token 可能来自两个地方：跳转地址里的 CAS ticket（通过浏览器发起的
``exchange-token`` 请求交换），或者门户页面的 ``localStorage``。两者都必须先通过
用户信息接口确认身份，才允许写入缓存。
"""

from __future__ import annotations

import json
import logging

from ..api.school import DeadlineExceeded, _redact_text, _remaining_seconds, get_student_id
from ..cache import save_cached_token
from .browser import _browser_timeout_ms
from .errors import FailureReason, LoginError
from .urls import _find_query_value_from_url, _is_school_host

logger = logging.getLogger("swu")


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


def _validate_and_cache_token(username, token, cache_path, timeout, session, deadline):
    """Cache only a token that identifies the logged-in school account."""
    if not isinstance(token, str) or not token:
        raise LoginError(FailureReason.TOKEN_EXTRACT, "登录成功后提取到的 Token 格式无效")
    # The exchange endpoint returning HTTP 200 is not enough to prove that a
    # candidate local-storage value is the portal access token.  Validate its
    # identity through the existing user API before persisting it.
    get_student_id(
        token,
        timeout=min(timeout, 5),
        session=session,
        deadline=deadline,
    )
    save_cached_token(username, token, cache_path)
    return token


def token_from_exchange_payload(result):
    """Return the access token from one ``exchange-token`` fetch result.

    The portal answers with a bare string, a ``{"data": ...}`` wrapper, or one
    of the loose ``token`` / ``access_token`` keys depending on the deployment,
    so all three shapes are accepted.  Anything else is reported as "no token"
    by the caller instead of being cached as a credential.
    """

    if not isinstance(result, dict) or not result.get("ok"):
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
    return None


def exchange_token_from_browser_page(page, ticket, timeout, deadline=None):
    if not ticket:
        return None
    _remaining_seconds(deadline)
    timeout_ms = _browser_timeout_ms(timeout, deadline)
    try:
        result = page.evaluate(
            """async ({ ticket, timeoutMs }) => {
            const url = `/gateway/fighter-middle/api/integrate/uaap/cas/exchange-token`
                + `?token=${encodeURIComponent(ticket)}&remember=true`;
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
    token = token_from_exchange_payload(result)
    if token is not None:
        return token
    if result.get("ok"):
        logger.debug(
            "浏览器 exchange-token 未返回 Token：HTTP %s %s",
            result.get("status"),
            _redact_text(result.get("text")),
        )
    else:
        logger.debug(
            "浏览器 exchange-token 请求未成功：HTTP %s %s",
            result.get("status"),
            _redact_text(result.get("text")),
        )
    return None
