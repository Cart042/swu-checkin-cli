"""统一认证跳转遗留的设备 Cookie 处理。

CAS 跳转会在登录主机上写入不透明设备 Cookie，带着它们提交登录表单会被认证服务
拒绝（HTTP 400），因此提交前必须精确清除、并保留其余会话 Cookie。
"""

from __future__ import annotations

import logging
import re

from ..api.school import _redact_text
from .debug import _debug_user_id

logger = logging.getLogger("swu")


# the browser then replays to the IDM host.
_OPAQUE_COOKIE_NAME = re.compile(r"^[0-9A-Za-z]{13}$")


def _cookie_reaches_login_host(cookie_domain, login_host):
    """Whether a cookie scoped to ``cookie_domain`` is sent to ``login_host``.

    Host-only cookies match exactly; a domain cookie matches the domain itself
    and every subdomain, which is why a device cookie issued for ``swu.edu.cn``
    would also be replayed to the login host.
    """
    domain = (cookie_domain or "").lstrip(".").rstrip(".").lower()
    if not domain:
        return False
    return login_host == domain or login_host.endswith("." + domain)


def _drop_federation_cookies(context, login_host, username):
    """Remove the CAS hop's opaque device cookies before submitting the form.

    ``uaaap.swu.edu.cn`` sets two opaque cookies that are replayed to
    ``idm.swu.edu.cn``.  While they are present, ``POST /am/UI/Login`` is
    answered with HTTP 400 and an empty body; after removing exactly those
    cookies the same POST reaches the authentication service and returns its
    normal result page.

    Deletion targets each cookie's exact ``name``/``domain`` pair, so the rest
    of the jar -- including the same cookie names on the CAS host -- stays
    intact.  Returns ``True`` when the jar is in the intended state.
    """
    host = (login_host or "").rstrip(".").lower()
    if not host:
        return False
    try:
        cookies = context.cookies()
    except Exception as exc:
        logger.warning("账号 %s: 读取浏览器 Cookie 失败：%s", _debug_user_id(username), _redact_text(exc))
        return False

    targets = sorted(
        {
            (cookie.get("domain") or "", cookie.get("name") or "")
            for cookie in cookies
            if _cookie_reaches_login_host(cookie.get("domain"), host)
            and _OPAQUE_COOKIE_NAME.match(cookie.get("name") or "")
        }
    )
    if not targets:
        return True

    for domain, name in targets:
        try:
            context.clear_cookies(name=name, domain=domain)
        except Exception as exc:
            # Everything else stays in the jar, so the caller can still retry
            # instead of continuing without any cookie at all.
            logger.warning(
                "账号 %s: 清除认证跳转遗留 Cookie 失败：%s",
                _debug_user_id(username),
                _redact_text(exc),
            )
            return False
    logger.debug(
        "账号 %s: 已清除认证跳转遗留 Cookie %s 个。",
        _debug_user_id(username),
        len(targets),
    )
    return True
