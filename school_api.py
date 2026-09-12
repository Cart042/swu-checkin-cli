"""HTTP transport and response handling for the SWU school APIs.

This module deliberately has no browser or login-page dependencies.  Keeping
the API boundary here means cached-token validation and ordinary school
requests can be tested without importing Playwright or starting a browser.
"""

import json
import logging
import re
import time
import urllib.parse

import requests

logger = logging.getLogger("swu")


class DeadlineExceeded(TimeoutError):
    """The operation could not finish before its monotonic deadline."""


class SwuRequestError(requests.exceptions.RequestException):
    """An HTTP response from a school endpoint was not successful."""

    def __init__(self, message, *, status_code=None, response=None, method=None, url=None):
        super().__init__(message, response=response)
        self.status_code = status_code
        self.response = response
        self.method = method
        self.url = url


class TokenInvalidError(SwuRequestError):
    """The school API rejected the current access token."""


class SwuBusinessError(RuntimeError):
    """The school API returned a successful HTTP response with bad data."""


_SENSITIVE_QUERY_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "idtoken1",
    "idtoken2",
    "idtoken3",
    "ticket",
    "code",
    "state",
    "authorization",
}
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|token|access_token|refresh_token|id_token|idtoken[123]|ticket|code|state|authorization)\b\s*[=:]\s*)([^\s&<>'\";,}]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")


def _redact_url(url):
    """Remove credentials and one-time values from a diagnostic URL."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(str(url))
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        safe_query = [
            (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value) for key, value in query_pairs
        ]
        fragment_pairs = urllib.parse.parse_qsl(parsed.fragment, keep_blank_values=True)
        safe_fragment = (
            urllib.parse.urlencode(
                [
                    (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
                    for key, value in fragment_pairs
                ]
            )
            if fragment_pairs
            else parsed.fragment
        )
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(safe_query),
                safe_fragment,
            )
        )
    except Exception:
        return _redact_text(str(url))


def _redact_text(value, secrets=()):
    """Return diagnostic text with credentials and bearer/query secrets removed."""
    if value is None:
        return ""
    text = str(value)
    candidates = sorted(
        {str(secret) for secret in secrets if secret is not None and str(secret)},
        key=len,
        reverse=True,
    )
    for secret in candidates:
        # Very short values are too broad for safe replacement.  Named fields
        # and bearer values are still redacted by the expressions below.
        if len(secret) >= 2:
            text = text.replace(secret, "[REDACTED]")
    text = _SENSITIVE_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return text


def create_school_session():
    """Create a direct requests session with environment proxies disabled."""
    session = requests.Session()
    session.trust_env = False
    session.proxies.clear()
    return session


def check_school_connectivity(timeout=5):
    """Probe the school site for diagnostics without login or cache changes."""
    session = create_school_session()
    try:
        response = session.get("https://of.swu.edu.cn/", timeout=timeout)
        return True, f"HTTP {response.status_code}"
    except requests.exceptions.SSLError as exc:
        return False, f"TLS/证书连接失败：{_redact_text(exc)}"
    except requests.exceptions.Timeout as exc:
        return False, f"连接学校官网超时：{_redact_text(exc)}"
    except requests.exceptions.ConnectionError as exc:
        return False, f"无法连接学校官网：{_redact_text(exc)}"
    except requests.exceptions.RequestException as exc:
        return False, f"请求学校官网失败：{_redact_text(exc)}"
    finally:
        close = getattr(session, "close", None)
        if close:
            close()


_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _remaining_seconds(deadline):
    if deadline is None:
        return None
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("操作已超过截止时间")
    return remaining


def _timeout_with_deadline(timeout, deadline):
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return timeout
    if timeout is None:
        return remaining
    if isinstance(timeout, (tuple, list)):
        return tuple(min(float(value), remaining) if value is not None else remaining for value in timeout)
    try:
        return min(float(timeout), remaining)
    except (TypeError, ValueError):
        return remaining


def _http_status_error(method, url, status_code, response):
    safe_url = _redact_url(url)
    message = f"{method.upper()} {safe_url} 返回 HTTP {status_code}"
    error_type = TokenInvalidError if status_code in {401, 403} else SwuRequestError
    return error_type(
        message,
        status_code=status_code,
        response=response,
        method=method.upper(),
        url=safe_url,
    )


def _sleep_before_retry(attempt, backoff_factor, deadline):
    delay = max(0.0, float(backoff_factor)) ** attempt
    remaining = _remaining_seconds(deadline)
    if remaining is not None:
        delay = min(delay, remaining)
    if delay:
        time.sleep(delay)


def request_with_retry(
    method,
    url,
    max_retries=3,
    backoff_factor=2,
    session=None,
    retryable=None,
    deadline=None,
    **kwargs,
):
    """Issue a school request with bounded retries and HTTP validation.

    Reads default to retries for transient failures.  Writes default to one
    attempt so a timeout cannot duplicate a check-in; callers may explicitly
    opt into retries for a safe idempotent operation.
    """
    method_upper = str(method).upper()
    if retryable is None:
        retryable = method_upper in _IDEMPOTENT_METHODS
    attempts = max(1, int(max_retries or 1)) if retryable else 1
    own_client = session is None
    client = session or create_school_session()

    try:
        for attempt in range(1, attempts + 1):
            call_kwargs = dict(kwargs)
            if deadline is not None:
                call_kwargs["timeout"] = _timeout_with_deadline(call_kwargs.get("timeout"), deadline)
            try:
                response = client.request(method, url, **call_kwargs)
                _remaining_seconds(deadline)
                status = getattr(response, "status_code", None)
                if status is not None:
                    try:
                        status = int(status)
                    except (TypeError, ValueError) as exc:
                        raise SwuRequestError(
                            f"{method_upper} {_redact_url(url)} 返回无效 HTTP 状态码",
                            method=method_upper,
                            url=_redact_url(url),
                            response=response,
                        ) from exc
                    if not 200 <= status < 300:
                        error = _http_status_error(method_upper, url, status, response)
                        if retryable and status in _RETRYABLE_HTTP_STATUS and attempt < attempts:
                            logger.warning(
                                "请求 HTTP %s，将在重试后再次请求（第 %s/%s 次）：%s",
                                status,
                                attempt + 1,
                                attempts,
                                _redact_url(url),
                            )
                            _sleep_before_retry(attempt, backoff_factor, deadline)
                            continue
                        raise error
                return response
            except DeadlineExceeded:
                raise
            except requests.exceptions.RequestException as exc:
                if deadline is not None and time.monotonic() >= float(deadline):
                    raise DeadlineExceeded("请求已超过截止时间") from exc
                can_retry_exception = isinstance(
                    exc,
                    (requests.exceptions.Timeout, requests.exceptions.ConnectionError),
                )
                if not retryable or not can_retry_exception or attempt >= attempts:
                    logger.error("请求失败：%s", _redact_text(exc))
                    raise
                logger.warning(
                    "请求异常，将在重试后再次请求（第 %s/%s 次）：%s",
                    attempt + 1,
                    attempts,
                    _redact_text(exc),
                )
                _sleep_before_retry(attempt, backoff_factor, deadline)
    finally:
        if own_client:
            close = getattr(client, "close", None)
            if close:
                close()


_SUCCESS_CODES = {0, 200, 1100, "0", "200", "1100"}
_TOKEN_INVALID_CODES = {401, 403, 4010, 40101, 40102, "401", "403", "4010", "40101", "40102"}


def _api_json(response, endpoint):
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise SwuBusinessError(f"{endpoint} 返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(body, dict):
        raise SwuBusinessError(f"{endpoint} 返回结构不是对象")

    code = body.get("code")
    message = body.get("msg", body.get("message", ""))
    message_text = str(message) if message is not None else ""
    token_hint = re.search(r"(?i)(token|登录已过期|登录失效|未授权|无效凭证|认证失败)", message_text)
    if code in _TOKEN_INVALID_CODES or token_hint:
        raise TokenInvalidError(
            f"{endpoint} Token 无效",
            status_code=401 if code is None else code,
            response=response,
        )
    if code is not None and code not in _SUCCESS_CODES:
        raise SwuBusinessError(f"{endpoint} 返回业务错误：code={code!r}, message={_redact_text(message_text)}")
    return body


def _session_student_cache(session):
    """Get a best-effort per-session token identity cache."""
    if session is None:
        return None
    try:
        cache = getattr(session, "_swu_student_id_cache", None)
        if cache is None:
            cache = {}
            session._swu_student_id_cache = cache
        return cache if isinstance(cache, dict) else None
    except Exception:
        # Some test doubles or custom sessions disallow arbitrary attributes;
        # identity caching is an optimization and must never break requests.
        return None


def get_student_id(token, timeout=10, session=None, deadline=None):
    """Validate *token* and return its student ID.

    When a caller reuses one session, a successful identity response is kept
    in memory for that session.  This lets token validation performed by login
    serve the immediately following check-in without a duplicate API call.
    """
    _remaining_seconds(deadline)
    cache = _session_student_cache(session)
    if cache is not None:
        try:
            cached = cache.get(token)
        except (TypeError, AttributeError):
            cached = None
        if isinstance(cached, str) and cached:
            _remaining_seconds(deadline)
            return cached

    url = "https://of.swu.edu.cn/gateway/fighter-middle/api/auth/user?appType=fighter-portal"
    headers = {"fighter-auth-token": token}
    response = request_with_retry(
        "GET",
        url,
        headers=headers,
        timeout=timeout,
        session=session,
        retryable=True,
        deadline=deadline,
    )
    body = _api_json(response, "用户信息接口")
    try:
        student_id = body["data"]["subject"]["username"]
    except (KeyError, TypeError) as exc:
        raise SwuBusinessError(f"用户信息接口返回结构异常: {exc}") from exc
    if not isinstance(student_id, (str, int)) or isinstance(student_id, bool) or not str(student_id):
        raise SwuBusinessError("用户信息接口返回的学号无效")
    student_id = str(student_id)
    if cache is not None:
        try:
            cache[token] = student_id
        except (TypeError, AttributeError):
            pass
    return student_id


def get_dormitory(token, timeout=10, session=None, deadline=None):
    url = "https://of.swu.edu.cn/gateway/fighter-baida/api/cqlc/getDormitory"
    headers = {"fighter-auth-token": token, "Content-Type": "application/json;charset=UTF-8"}
    response = request_with_retry(
        "POST",
        url,
        headers=headers,
        data=json.dumps({}),
        timeout=timeout,
        session=session,
        retryable=True,
        deadline=deadline,
    )
    return _api_json(response, "宿舍信息接口")


def get_transition_today(token, timeout=10, session=None, deadline=None):
    url = "https://of.swu.edu.cn//gateway/fighter-baida/api/cqtj/getTransitionByToday"
    headers = {"fighter-auth-token": token}
    data = {"pageNum": 1, "pageSize": 1}
    response = request_with_retry(
        "POST",
        url,
        headers=headers,
        data=data,
        timeout=timeout,
        session=session,
        retryable=True,
        deadline=deadline,
    )
    body = _api_json(response, "今日签到任务接口")
    try:
        records = body["data"]["records"]
    except (KeyError, TypeError) as exc:
        raise SwuBusinessError(f"今日签到任务接口返回结构异常: {exc}") from exc
    if not isinstance(records, list):
        raise SwuBusinessError("今日签到任务接口 records 不是数组")
    record = records[0] if records else None
    if record is not None and not isinstance(record, dict):
        raise SwuBusinessError("今日签到任务接口记录不是对象")
    return record


__all__ = [
    "DeadlineExceeded",
    "SwuBusinessError",
    "SwuRequestError",
    "TokenInvalidError",
    "check_school_connectivity",
    "create_school_session",
    "get_dormitory",
    "get_student_id",
    "get_transition_today",
    "request_with_retry",
]
