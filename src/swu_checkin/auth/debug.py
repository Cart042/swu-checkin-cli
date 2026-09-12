"""脱敏的登录诊断记录。

登录页 HTML 里可能带凭据和一次性票据，因此这里只写 URL 元数据、选择器存在性
和脱敏后的错误文本，不保存页面内容或截图。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from ..api.school import _redact_text, _redact_url

logger = logging.getLogger("swu")


def _debug_user_id(username):
    digest = hashlib.sha256(str(username).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"user-{digest}"


def save_login_debug_artifacts(page, username, reason, error=None):
    """Write a small, redacted diagnostic record.

    Login pages contain credentials in form controls and sometimes include
    one-time tickets in their URL.  We intentionally do not persist page HTML
    or screenshots.  The text record contains only URL metadata, selector
    presence, and redacted error information.
    """
    debug_dir = os.getenv("SWU_DEBUG_DIR", "").strip()
    if not debug_dir:
        return
    safe_user = _debug_user_id(username)
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reason))[:40] or "debug"
    prefix = os.path.join(debug_dir, f"login_{safe_user}_{safe_reason}_{int(time.time() * 1000)}")
    try:
        os.makedirs(debug_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(debug_dir, 0o700)
        except OSError:
            pass
        page_url = _redact_url(getattr(page, "url", ""))
        try:
            title = _redact_text(page.title(), [username])
        except Exception:
            title = ""
        selectors = (
            "input#loginName, input[name=IDToken1]",
            "input#password, input[name=IDToken2]",
            "input#validateCode, input[name=IDToken3]",
            "button:has-text(登录), input[type=submit]",
        )
        selector_state: dict[str, bool | None] = {}
        for selector in selectors:
            try:
                selector_state[selector] = bool(page.locator(selector).count())
            except Exception:
                selector_state[selector] = None
        lines = [
            f"reason: {_redact_text(reason, [username])}",
            f"url: {page_url}",
            f"title: {title}",
            f"error_type: {type(error).__name__ if error is not None else ''}",
            f"error: {_redact_text(error, [username])[:1000] if error is not None else ''}",
            "selectors: " + json.dumps(selector_state, ensure_ascii=False, sort_keys=True),
        ]
        with open(f"{prefix}.txt", "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(f"{prefix}.txt", 0o600)
        except OSError:
            pass
        logger.info("账号 %s: 已保存登录诊断：%s.txt", safe_user, prefix)
    except Exception as exc:
        logger.warning("账号 %s: 保存登录诊断失败：%s", safe_user, _redact_text(exc, [username]))
