"""Shared status text for the CLI runner and check-in service."""

STATUS_MESSAGES = {
    0: "今日暂无签到任务。",
    1: "签到成功。",
    2: "今日已签到，无需重复操作。",
    3: "账号或密码验证失败，请检查后重试。",
    4: "连接错误或请求超时，请稍后重试。",
    5: "请假中，请检查是否有打卡任务。",
    6: "登录页加载失败或超时，可能是学校服务或网络异常。",
    7: "验证码连续识别失败，请稍后重试或使用 --force-login。",
    8: "登录成功但 Token 提取失败，可能是页面结构变化。",
    9: "学校登录页结构可能变化，请更新脚本选择器。",
    10: "学校接口返回异常，可能是服务暂时不可用。",
    11: "Token 校验失败或已失效，请尝试 --force-login。",
}

LOGIN_REASON_STATUS = {
    "credential": 3,
    "page_load": 6,
    "captcha": 7,
    "token_extract": 8,
    "login_page_changed": 9,
}
