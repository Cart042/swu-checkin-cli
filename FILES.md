# 文件备注

本文件记录仓库中公开文件的用途，方便部署和排查问题。

| 文件 | 用途 |
| --- | --- |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | GitHub 问题反馈模板，引导提交运行环境和关键日志。 |
| `.github/ISSUE_TEMPLATE/config.yml` | GitHub Issue 模板配置。 |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | GitHub 功能建议模板。 |
| `.github/PULL_REQUEST_TEMPLATE.md` | Pull Request 模板，提醒说明变更、验证和敏感信息检查。 |
| `.github/workflows/swu-check.yml` | GitHub Actions 定时和手动签到工作流，使用浏览器登录；手动调试开关启用时上传短期保留的脱敏文本 artifact。 |
| `.github/workflows/pr-ci.yml` | 离线 Pull Request 检查，运行 Python 源码编译、单元测试和命令行帮助，不访问学校网站。 |
| `.dockerignore` | 排除账号、环境变量、Token 缓存、日志、调试文件、运行锁、Git 元数据和 Python 缓存，避免进入镜像。 |
| `.env.example` | 环境变量模板，包含账号、多账号、并发、重试、代理、按需调试和推送配置示例。 |
| `.gitignore` | 忽略账号、`.env`、Token 缓存、日志、调试目录、运行锁、Python 缓存和原子写入临时文件。 |
| `Dockerfile` | Python 3.11 镜像构建文件，安装依赖和 Playwright Chromium，默认使用 `/data` 保存配置。 |
| `LICENSE` | MIT License。 |
| `README.md` | 安装、账号配置、数字菜单、GitHub Actions、Docker、代理、推送、环境变量和状态码说明。 |
| `check_in.py` | 主程序入口，负责账号读取、配置检查、数字菜单、签到流程、运行锁和推送汇总。 |
| `docker-compose.yml` | Docker Compose 部署配置，挂载 `./data` 到容器 `/data` 并运行一次性签到任务。 |
| `get_info.py` | 浏览器登录、验证码识别、脱敏登录诊断文本、Token 获取/缓存、代理和学校接口请求。 |
| `notify.py` | 钉钉、企业微信、Bark、Server 酱和 PushDeer 推送。 |
| `requirements.txt` | Python 直接依赖列表。 |
| `runner.py` | 多账号并发、有限重试、deadline 和结果汇总。 |
| `tests/test_check_in.py` | 不访问网络的签到、配置和重试边界测试。 |
| `tests/test_login_api.py` | 不访问网络的登录、Token 缓存和 API 请求边界测试。 |
| `users.json.example` | 多账号配置格式示例，不包含真实凭据。 |
