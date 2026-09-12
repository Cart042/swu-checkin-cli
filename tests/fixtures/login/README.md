# 登录链页面样本

这个目录保存登录链的**脱敏样本**，供 `tests/test_login_fixtures.py` 驱动
`swu_checkin.auth` 里的解析与分类函数。

## 样本来源与脱敏规则

- 样本按学校页面（统一认证 IDM 登录页、认证服务的错误提示、CAS 跳转地址、
  `exchange-token` 响应、门户 `localStorage`、跳转产生的 Cookie 列表）的
  **已观测结构**重建，删除了所有真实内容。
- 里面的账号、密码、票据、Cookie 和 Token 全部是占位值，形如
  `example-not-real` / `0000`，不对应任何真实凭据或会话。
- 页面样本只保留与选择器和提示文案有关的结构，不包含内嵌脚本、样式表、
  统计代码或任何后端地址。
- 提交新样本前请确认：不含真实账号、密码、Cookie、Token、票据、IP 或内网域名。

## 各文件用途

| 文件 | 用途 |
| --- | --- |
| `idm_login_form.html` | 统一认证登录页：用户名密码 tab、账号/密码/验证码输入框、验证码图片、统一认证按钮和提交按钮。 |
| `waf_400.html` | 设备 Cookie 未清除时认证服务返回的空响应页，用于确认它不会被误判成登录表单。 |
| `captcha_error_popup.html` | 验证码错误的弹窗结构（`.pop .ctnTxt` + `.pop .confirm`）。 |
| `credential_error_popup.html` | 账号或密码错误的弹窗结构。 |
| `device_verify_failed.html` | “动态口令验证失败”错误页，含“返回至登录页面”链接。 |
| `cas_redirects.json` | 登录链各跳转地址：CAS 入口、IDM 表单、OAuth 跳转（http/https 两种形态）和带 ticket 的回调。 |
| `exchange_token_success.json` | `exchange-token` 成功响应，覆盖字符串和对象两种 Token 形态。 |
| `exchange_token_failure.json` | `exchange-token` 被拦截时的响应结构。 |
| `portal_localstorage.json` | 门户 `localStorage` 导出，含 vuex 包装的 `access_token` 与其他 Token 字段。 |
| `federation_cookies.json` | 跳转后浏览器 Cookie 列表：CAS 主机的不透明设备 Cookie、会话 Cookie，以及其他主机的同名 Cookie。 |

## 学校改版时怎么用

1. 把新的（脱敏后的）页面或响应替换到对应文件；
2. 运行 `python -m unittest discover -s tests -t .`；
3. `tests/test_login_fixtures.py` 会指出是选择器、错误文案、跳转地址还是 Token
   形态发生了变化，再决定改 `auth/pages.py`、`auth/urls.py` 还是 `auth/tokens.py`。
