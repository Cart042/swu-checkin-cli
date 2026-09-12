"""登录页面文案与选择器的纯函数判定。

这些函数不导入 Playwright，所以 ``tests/fixtures/login/`` 里的脱敏样本可以直接
驱动它们：学校改了提示文案、表单字段或验证码地址时，
``tests/test_login_fixtures.py`` 会先失败，而不是等到线上登录失败才发现。
选择器常量是运行时代码的唯一来源，判定函数复用同一份常量。
"""

from __future__ import annotations

import re
import urllib.parse

# 登录表单字段。IDM 使用 IDToken1..3，门户改版后出现过 loginName/validateCode。
LOGIN_NAME_SELECTOR = (
    'input#loginName, input[name="IDToken1"], input[placeholder*="用户名"], input[placeholder*="账号"]'
)
PASSWORD_SELECTOR = 'input#password, input[name="IDToken2"], input[type="password"], input[placeholder*="密码"]'
CAPTCHA_INPUT_SELECTOR = (
    'input#validateCode, input[name="IDToken3"], input[placeholder*="验证码"], input[placeholder*="校验码"]'
)
CAPTCHA_IMAGE_SELECTOR = 'img#kaptchaImage, img[src*="kaptcha"], img[src*="captcha"]'
SUBMIT_BUTTON_SELECTOR = 'input#button, button:has-text("登录"), input[type="submit"], .loginBtn, .btn-login'
UNIFIED_LOGIN_BUTTON_SELECTOR = 'img[src*="unified_button"]'
USERNAME_PASSWORD_TAB_SELECTOR = 'text="用户名密码"'
BACK_TO_LOGIN_LINK_SELECTOR = 'a:has-text("返回至登录页面")'

# 页面内的提示与弹窗。
ERROR_MESSAGE_SELECTOR = ".pop .ctnTxt, .error, #error, .errorMessage, #errorMessage, .messager-body"
CONFIRM_BUTTON_SELECTOR = ".pop .confirm"

# 认证服务在验证码/凭据错误时返回的文案特征。
DEVICE_VERIFICATION_FAILURE_MARKERS = ("动态口令验证失败", "验证失败")
CREDENTIAL_ERROR_MARKERS = ("密码", "账户", "用户名", "动态口令", "不正确")
CAPTCHA_ERROR_MARKER = "验证码"

_LOGIN_NAME_RE = re.compile(r"""id=["']?loginName|name=["']?IDToken1""", re.IGNORECASE)
_PASSWORD_RE = re.compile(r"""id=["']?password|name=["']?IDToken2|type=["']?password""", re.IGNORECASE)
_CAPTCHA_IMAGE_RE = re.compile(r"""id=["']?kaptchaImage|kaptcha|captcha""", re.IGNORECASE)


def is_device_verification_failure(body_text: str) -> bool:
    """页面是否停留在统一认证的“验证失败”错误页。"""

    return any(marker in body_text for marker in DEVICE_VERIFICATION_FAILURE_MARKERS)


def is_captcha_error(error_text: str) -> bool:
    """提示文案是否指向验证码，而不是账号密码。"""

    return CAPTCHA_ERROR_MARKER in error_text


def is_credential_error(error_text: str) -> bool:
    """提示文案是否指向账号或密码错误。

    只要文案同时提到验证码，就按验证码错误处理：IDM 的验证码错误提示里也会出现
    “密码”字样，误判成凭据错误会让脚本直接放弃重试。
    """

    if is_captcha_error(error_text):
        return False
    return any(marker in error_text for marker in CREDENTIAL_ERROR_MARKERS)


def is_captcha_resource(url: str) -> bool:
    """图片资源是否必须放行给页面。

    登录过程中只有验证码图片和统一认证按钮图片是必需的，其余图片会被
    ``route_login_resource`` 拦截以减少流量。验证码图片地址会随会话变化，
    因此按文件名和固定路径判断，而不是按完整 URL 比对。
    """

    if "kaptchaImage" in url or "unified_button" in url:
        return True
    return urllib.parse.urlsplit(url).path == "/am/validate.code"


def has_login_form(html: str) -> bool:
    """HTML 里是否同时存在账号和密码输入框。"""

    return bool(_LOGIN_NAME_RE.search(html)) and bool(_PASSWORD_RE.search(html))


def has_captcha_image(html: str) -> bool:
    """HTML 里是否还有验证码图片。"""

    return bool(_CAPTCHA_IMAGE_RE.search(html))


_LOGIN_ERROR_MESSAGE_JS = """() => {
    const seen = new Set();
    const nodes = document.querySelectorAll(
        '.pop .ctnTxt, .error, #error, .errorMessage, #errorMessage, .messager-body'
    );
    for (const node of nodes) {
        if (!node.getClientRects().length) {
            continue;
        }
        const text = (node.innerText || '').trim();
        if (text) {
            seen.add(text);
        }
    }
    return Array.from(seen).join('\\n');
}"""

_LOGIN_ERROR_MESSAGE_SELECTOR = ".pop .ctnTxt, .error, #error, .errorMessage, #errorMessage, .messager-body"

# Some rejections are rendered as ordinary page text instead of the dialog
# node, so a bounded excerpt around a failure-specific phrase is used as a
# fallback.  The hints stay narrow on purpose: static labels on the form, such
# as the "用户名密码" tab, must not look like an error message.
_LOGIN_FAILURE_HINTS = (
    "验证失败",
    "动态口令验证失败",
    "用户名或密码",
    "密码错误",
    "密码不正确",
    "账号已被",
    "锁定",
    "不正确",
)
