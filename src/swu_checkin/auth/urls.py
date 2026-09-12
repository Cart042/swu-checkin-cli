"""登录跳转地址的纯解析函数。

这些函数不接触浏览器，因此可以被脱敏的跳转样本直接驱动测试。
"""

from __future__ import annotations

import urllib.parse


def _absolute_redirect_target(response, location):
    """Resolve a login response's ``Location`` against its own URL.

    The login response points at the plain-HTTP form of the authorize URL,
    whose redirect loses the authenticated session and bounces back to the
    login form, so replaying the same URL over HTTPS reaches the portal.  A
    relative ``Location`` is resolved first, which keeps that upgrade -- and
    the recovery path that uses this value -- applicable to it as well.
    """
    if not location:
        return None
    base = getattr(response, "url", "") or ""
    target = urllib.parse.urljoin(base, location) if base else location
    if target.startswith("http://"):
        target = "https://" + target[len("http://") :]
    return target


def _find_query_value_from_url(url, key):
    parsed = urllib.parse.urlparse(url)
    for part in [parsed.query, parsed.fragment]:
        values = urllib.parse.parse_qs(part).get(key)
        if values:
            return urllib.parse.unquote(values[0])
        if "?" in part:
            values = urllib.parse.parse_qs(part.split("?", 1)[1]).get(key)
            if values:
                return urllib.parse.unquote(values[0])
    if f"{key}=" in url:
        return urllib.parse.unquote(url.split(f"{key}=", 1)[1].split("&", 1)[0])
    return None


def _url_hostname(url):
    """Return a normalized URL hostname without inspecting query strings."""
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").rstrip(".").lower()
    except (TypeError, ValueError):
        return ""


def _is_school_host(url):
    """Whether ``url`` is hosted by the school's portal domain."""
    hostname = _url_hostname(url)
    return hostname == "of.swu.edu.cn" or hostname.endswith(".of.swu.edu.cn")
