"""School check-in operations.

This module is the runtime boundary: importing it loads requests and the
browser/API facade.  The lightweight command-line module imports it only for
an actual check-in or an explicit network diagnostic.
"""

import json
import logging
import time

import requests

from get_info import get_token
from school_api import (
    DeadlineExceeded,
    SwuBusinessError,
    SwuRequestError,
    TokenInvalidError,
    check_school_connectivity,
    create_school_session,
    get_dormitory,
    get_student_id,
    get_transition_today,
    request_with_retry,
)
from status import LOGIN_REASON_STATUS, STATUS_MESSAGES

logger = logging.getLogger("swu.check_in")

def _api_body(response, endpoint):
    """Decode an API body and turn malformed payloads into business failures."""
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise SwuBusinessError(f"{endpoint} 返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(body, dict):
        raise SwuBusinessError(f"{endpoint} 返回结构不是对象")
    return body


def _is_leave_result(body):
    """Recognize only an explicit leave flag or exact status text.

    A generic error mentioning leave must remain a business error instead of
    being reported as the benign ``请假中`` status.
    """
    for key in ("isVacation", "is_vacation", "vacation", "hasLeave", "has_leave", "leave"):
        value = body.get(key)
        if isinstance(value, bool):
            return value
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("isVacation", "is_vacation", "vacation", "hasLeave", "has_leave", "leave"):
            value = data.get(key)
            if isinstance(value, bool):
                return value
    for key in ("message", "msg", "status"):
        value = body.get(key)
        if isinstance(value, str) and value.strip() in {"请假中", "当前处于请假状态"}:
            return True
    return None


def vacation_enable(token, timeout, session, deadline=None):
    headers = {"fighter-auth-token": token}
    url = "https://of.swu.edu.cn/gateway/fighter-baida/api/flow-ext/start-process-instance-by-key"
    params = {"processDefinitionKey": "XSQJXJ"}
    response = request_with_retry(
        "POST",
        url,
        headers=headers,
        params=params,
        json={},
        timeout=timeout,
        session=session,
        retryable=False,
        deadline=deadline,
    )
    body = _api_body(response, "请假状态接口")
    code = body.get("code")
    if code in {200, 1100, "200", "1100"}:
        return False
    leave_result = _is_leave_result(body)
    if leave_result is True:
        return True
    raise SwuBusinessError(f"请假状态接口返回未知业务结果：code={code!r}")


def _checkin_payload(token, timeout, session, transition_today, deadline=None):
    try:
        formid = transition_today["formId"]
        record_id = transition_today["id"]
        dormitory_body = get_dormitory(token, timeout, session=session, deadline=deadline)
        dormitory = dormitory_body["data"]["columnList"]
        student_id = get_student_id(token, timeout=timeout, session=session, deadline=deadline)
        return {
            "id": record_id,
            "formId": formid,
            "tsrq": time.strftime("%Y-%m-%d"),
            "xh": student_id,
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
                "tip": "当前在签到范围内",
            },
        }, formid
    except (KeyError, IndexError, TypeError) as exc:
        raise SwuBusinessError(f"签到所需的宿舍或任务结构异常: {exc}") from exc


def checkin_post(token, timeout, session, transition_today, deadline=None):
    """Submit once, then verify state; never blindly re-submit after a timeout."""
    if transition_today is None:
        return None
    payload, formid = _checkin_payload(token, timeout, session, transition_today, deadline=deadline)
    headers = {"fighter-auth-token": token, "Content-Type": "application/json;charset=UTF-8"}
    url = "https://of.swu.edu.cn/gateway/fighter-baida/api/form-instance/save"
    params = {"formId": formid, "isSubmitProcess": False}

    try:
        response = request_with_retry(
            "POST",
            url,
            headers=headers,
            params=params,
            data=json.dumps(payload),
            timeout=timeout,
            session=session,
            retryable=False,
            deadline=deadline,
        )
        body = _api_body(response, "签到提交接口")
        code = body.get("code")
        if code is not None and code not in {0, 200, 1100, "0", "200", "1100"}:
            raise SwuBusinessError(f"签到提交接口返回业务失败：code={code!r}, message={body.get('msg', body.get('message', ''))}")
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, DeadlineExceeded) as exc:
        logger.warning("签到写请求结果未知，先查询今日状态，不重复提交：%s", exc)
        try:
            after_timeout = get_transition_today(token, timeout, session=session, deadline=deadline)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, DeadlineExceeded) as query_exc:
            logger.error("签到写请求超时且复查失败：%s", query_exc)
            return 4
        if _same_checked_in_record(after_timeout, transition_today):
            return 1
        return 4

    # A successful HTTP response is not proof of a successful business action.
    try:
        after_submit = get_transition_today(token, timeout, session=session, deadline=deadline)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, DeadlineExceeded) as exc:
        logger.error("签到提交后复查失败：%s", exc)
        return 4
    if _same_checked_in_record(after_submit, transition_today):
        return 1
    raise SwuBusinessError("签到提交接口未在复查中确认已签到")


def _same_checked_in_record(candidate, submitted_record):
    if (
        not isinstance(candidate, dict)
        or not isinstance(submitted_record, dict)
        or candidate.get("qdzt") != "已签到"
    ):
        return False
    return (
        str(candidate.get("id")) == str(submitted_record.get("id"))
        and str(candidate.get("formId")) == str(submitted_record.get("formId"))
    )


def check_in(username: str, password: str, timeout: int = 10, force_login: bool = False, deadline=None):
    if requests is None:
        logger.error("requests 依赖未安装")
        return 10
    session = create_school_session()
    try:
        try:
            token = get_token(
                username,
                password,
                timeout,
                session=session,
                force_login=force_login,
                deadline=deadline,
            )
        except Exception as exc:
            reason = getattr(exc, "reason", "unknown")
            status = LOGIN_REASON_STATUS.get(reason, 11 if isinstance(exc, TokenInvalidError) else 10)
            logger.error("登录失败（%s）: %s", STATUS_MESSAGES.get(status, "未知原因"), exc)
            return status

        try:
            if vacation_enable(token, timeout, session=session, deadline=deadline):
                return 5
            transition_today = get_transition_today(token, timeout, session=session, deadline=deadline)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, DeadlineExceeded) as exc:
            logger.error("学校接口连接失败或超时: %s", exc)
            return 4
        except TokenInvalidError:
            return 11
        except (SwuRequestError, SwuBusinessError) as exc:
            logger.error("学校接口失败: %s", exc)
            return 10
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("学校接口返回结构异常: %s", exc)
            return 10
        except Exception as exc:
            logger.error("学校接口请求异常: %s", exc)
            return 10

        if not transition_today:
            return 0
        if transition_today.get("qdzt") == "已签到":
            return 2
        try:
            return checkin_post(token, timeout, session=session, transition_today=transition_today, deadline=deadline)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, DeadlineExceeded) as exc:
            logger.error("签到连接失败或超时: %s", exc)
            return 4
        except TokenInvalidError:
            return 11
        except (SwuRequestError, SwuBusinessError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("签到业务失败: %s", exc)
            return 10
        except Exception as exc:
            logger.error("签到执行异常: %s", exc)
            return 10
    finally:
        session.close()
