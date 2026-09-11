# 文件备注

本文件记录仓库中公开文件的用途，方便部署和排查问题。

| 文件 | 用途 |
| --- | --- |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | GitHub 问题反馈模板，引导提交运行环境和关键日志。 |
| `.github/ISSUE_TEMPLATE/config.yml` | GitHub Issue 模板配置。 |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | GitHub 功能建议模板。 |
| `.github/PULL_REQUEST_TEMPLATE.md` | Pull Request 模板，提醒说明变更、验证和敏感信息检查。 |
| `.github/workflows/swu-check.yml` | GitHub Actions 定时和手动签到工作流，使用缓存的 Python 依赖和 Chromium headless shell 登录；手动调试开关启用时上传短期保留的脱敏文本 artifact。 |
| `.github/workflows/pr-ci.yml` | Pull Request 运行时检查，按 `requirements.txt` 缓存并安装完整依赖，运行 Python 源码编译、单元测试、命令行帮助和本地运行时冒烟，不访问学校网站。 |
| `.dockerignore` | 排除账号、环境变量、Token 缓存、日志、调试文件、运行锁、Git 元数据和 Python 缓存，避免进入镜像。 |
| `.env.example` | 环境变量模板，包含账号、多账号、并发、重试、按需调试和推送配置示例。 |
| `.gitignore` | 忽略账号、`.env`、Token 缓存、日志、调试目录、运行锁、Python 缓存和原子写入临时文件。 |
| `Dockerfile` | Python 3.11 镜像构建文件，安装依赖和 Playwright Chromium headless shell，默认使用 `/data` 保存配置。 |
| `LICENSE` | MIT License。 |
| `README.md` | 安装、账号配置、数字菜单、GitHub Actions、Docker、网络、推送、环境变量和状态码说明。 |
| `check_in.py` | 主程序入口，负责 CLI、配置检查、运行锁和推送汇总；仅在实际操作时加载签到服务。 |
| `checkin_service.py` | 单账号签到流程、请假状态、任务查询、提交复查和结果状态映射。 |
| `status.py` | CLI、runner 与签到服务共用的状态码和登录失败原因文本。 |
| `config.py` | 账号来源优先级、统一校验、dotenv 加载、账号文件和推送配置读写。 |
| `menu.py` | 数字配置菜单和交互式账号、并发、推送与缓存操作；不加载浏览器运行时。 |
| `docker-compose.yml` | Docker Compose 部署配置，挂载 `./data` 到容器 `/data` 并运行一次性签到任务。 |
| `cache.py` | Token 缓存的原子读写和进程内并发保护。 |
| `get_info.py` | 浏览器登录、验证码识别、脱敏登录诊断文本和 Token 获取。 |
| `login.py` | 延迟加载 Playwright 和登录错误类型。 |
| `school_api.py` | 学校 HTTP 会话、网络诊断、请求重试和学校接口数据。 |
| `notify.py` | 钉钉、企业微信、Bark、Server 酱、PushDeer 和 Telegram 推送。 |
| `requirements.txt` | Python 直接运行依赖列表，供签到运行时和 CI 运行时冒烟检查使用。 |
| `runner.py` | 多账号并发、有限重试、deadline 和结果汇总。 |
| `scripts/smoke_runtime.py` | 独立运行时冒烟检查：启动本地页面和无 channel 的 headless Chromium，并用合成无敏感图片验证 ddddocr；不访问学校网络或执行签到。 |
| `scripts/profile_login.py` | 交互式只读登录和 Token 缓存性能测量；通过标准输入接收凭据，仅输出脱敏的阶段耗时和进程资源摘要，不执行签到或推送。 |
| `docs/performance.md` | 性能测量命令、采样口径、凭据安全和当前环境限制说明。 |
| `tests/test_check_in.py` | 不访问网络的签到、配置和重试边界测试。 |
| `tests/test_config.py` | 账号优先级、dotenv 加载和配置错误测试。 |
| `tests/test_menu.py` | 不访问网络的菜单文件操作测试。 |
| `tests/test_login_api.py` | 不访问网络的登录、Token 缓存和 API 请求边界测试。 |
| `tests/test_notify.py` | 不访问网络的推送渠道、Telegram 分段和重试边界测试。 |
| `users.json.example` | 多账号配置格式示例，不包含真实凭据。 |
