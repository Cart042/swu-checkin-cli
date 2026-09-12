# 统一认证登录排查

本文件记录浏览器登录链路的已知失败特征、代码中的处理位置和可调开关。
内容来自对真实账号的只读验证，不含账号、密码、Token、Cookie 值或验证码内容。

## 链路

`of.swu.edu.cn` 的联邦登录入口 → `uaaap.swu.edu.cn`（CAS，点击「统一认证登录」）
→ `idm.swu.edu.cn/am/UI/Login`（用户名密码 + 图形验证码）→ 登录 POST → 授权跳转
→ 门户 `of.swu.edu.cn` 的 CAS ticket → 交换 Token → 写入缓存。

## 已知失败特征

| 现象 | 原因 | 处理位置 |
| --- | --- | --- |
| 联邦跳转返回 HTTP 400 | 无头 Chromium 默认 UA 含 `HeadlessChrome`，被前置 WAF 拒绝 | `_login_user_agent()` 生成普通 Chrome UA（版本取自运行中的浏览器，平台串与运行环境一致） |
| 登录 POST 返回 HTTP 400、响应体为空 | 登录域上由 CAS 跳转带入的不透明设备 Cookie 被判为异常请求 | `_drop_federation_cookies()` 在提交前按 `name`+`domain` 精确删除 |
| 登录成功后的授权跳转返回 HTTP 400 | 登录响应重新下发设备 Cookie，且跳转目标为明文 HTTP | 解析 `Location`（相对地址先 `urljoin`）、再次清理设备 Cookie、改用 HTTPS 重跳；失败时由 `_recover_blocked_oauth_hop()` 兜底 |
| 登录表单不提交 | 验证码输入框的 `keyup` 会触发 `verifyCode()` 预校验，该接口当前恒返回 400，页面全局 `state` 被置为 false | 验证码一次性 `fill()`，不逐键输入、不清空 |
| 验证码图片始终不显示 | 图片资源被路由过滤器拦截 | `route_login_resource()` 放行 `/am/validate.code` 与验证码/按钮图片 |
| 认证页面返回 4xx/5xx | 前置 WAF 或认证服务临时拒绝（实测可复现约 15 分钟的窗口） | 加载与表单确认阶段各自重试，重试耗尽后才按 `page_load`（或 `waf_blocked`）上报 |

## 设备 Cookie 规则

- 识别规则：登录域（或它所属的父域）上「13 位纯字母数字」名称的 Cookie。
- 删除方式：按 `name` + `domain` 精确删除（`BrowserContext.clear_cookies`），
  不会清空整个 Cookie jar，CAS 会话与 `SESSION`、`AMAuthCookie`、
  `iPlanetDirectoryPro`、`amlbcookie` 等会话 Cookie 保持不变。
- 学校若更换命名长度或改挂父域，规则需要同步更新；登录 POST 返回 400 时日志会给出
  「符合统一认证前置拦截特征」的提示。

## 可调开关

| 环境变量 | 作用 |
| --- | --- |
| `SWU_LOGIN_UA` | 直接指定浏览器 User-Agent，覆盖自动生成的字符串（例如需要固定为某个已验证值时） |
| `SWU_LOG_LEVEL` | `DEBUG` 时会输出每一次跳转、Cookie 清理数量和重试原因，便于定位 |
| `SWU_DEBUG_DIR` | 登录失败时保存脱敏诊断文本的目录 |

## 登录失败原因与状态码

| `LoginError.reason` | 状态码 | 含义 |
| --- | ---: | --- |
| `credential` | 3 | 账号或密码校验失败（含认证服务返回的凭据类提示） |
| `page_load` | 6 | 登录页加载失败、超时，或重试耗尽后仍返回 HTTP 错误页 |
| `waf_blocked` | 6 | 登录 POST 持续被前置拦截（HTTP 400），通常是风控窗口或设备 Cookie 规则失效 |
| `captcha` | 7 | 验证码识别失败，或验证码图片未能渲染 |
| `token_extract` | 8 | 登录成功但未能从页面提取 Token |
| `login_page_changed` | 9 | 登录页结构变化，未找到表单或入口按钮 |
| `unknown` | 10 | 其它浏览器层异常 |

## 验证边界与运维注意

- 已用真实账号只读验证：冷启动取得 Token、只读学生身份接口通过、缓存命中不启动浏览器。
- 未验证：真实签到提交、请假、推送、Docker 整镜构建、GitHub Actions 运行。
- 短时间高频自动化登录会触发前置风控，登录 POST 会被持续拒绝（实测约 15 分钟窗口）。
  正常每天 1–2 次运行不受影响；诊断时请避免连续重试。
- 验证码识别并不稳定（实测抽样约 2/3 正确），因此重试路径必须保持可继续执行，
  不要把单次失败当作致命错误。
