# 文件备注

本文件记录仓库中每个公开文件的用途，方便部署、维护和排查问题。

| 文件 | 备注 |
| --- | --- |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | GitHub 问题反馈模板，引导用户提交运行环境、关键日志和自查确认。 |
| `.github/ISSUE_TEMPLATE/config.yml` | GitHub Issue 模板配置，保留空白 Issue 并提供 README 使用文档入口。 |
| `.github/ISSUE_TEMPLATE/feature_request.yml` | GitHub 功能建议模板，引导用户说明使用场景和期望方案。 |
| `.github/PULL_REQUEST_TEMPLATE.md` | GitHub Pull Request 模板，提醒说明变更内容、验证方式和敏感信息检查。 |
| `.github/workflows/swu-check.yml` | GitHub Actions 手动运行配置，默认使用浏览器登录链路，并在失败时上传登录页截图和 HTML 调试文件。 |
| `.dockerignore` | Docker 构建忽略规则，防止本地账号配置、环境变量、Token 缓存、Git 元数据和 Python 缓存进入镜像。 |
| `.env.example` | 环境变量配置模板，包含账号、并发数、日志级别、登录方式、学校官网代理自动发现和各类推送通道示例。 |
| `.gitignore` | Git 忽略规则，防止提交 `users.json`、`.env`、`.token_cache.json` 和 Python 缓存。 |
| `Dockerfile` | Docker 镜像构建文件，安装 Python 依赖、Playwright Chromium、系统运行库，并默认使用 `/data` 保存配置。 |
| `LICENSE` | 项目开源许可证，声明 MIT License 和版权信息。 |
| `README.md` | 项目使用文档，说明安装、配置检查、数字菜单、Docker、双登录链路、海外服务器网络边界、代理自动发现、定时任务、推送和状态码。 |
| `check_in.py` | 主程序入口，负责账号读取、配置检查、数字菜单、学校官网代理配置、签到流程、运行锁、错误信息细化和推送汇总。 |
| `des.py` | DES 加解密辅助模块，保留统一身份认证相关加密逻辑。 |
| `docker-compose.yml` | Docker Compose 部署配置，适合 VPS 上挂载 `./data` 目录并运行一次性打卡任务。 |
| `get_info.py` | 登录与接口模块，负责历史纯 HTTP 登录、Playwright 登录、登录页调试快照、验证码识别、Token 获取/缓存、代理自动发现、学校官网连通性检查、宿舍和今日任务接口读取。 |
| `notify.py` | 推送模块，支持钉钉、企业微信、Bark、Server 酱和 PushDeer。 |
| `requirements.txt` | Python 依赖列表，声明 `requests`、`PySocks`、`playwright`、`ddddocr`、`python-dotenv`。 |
| `users.json.example` | 多账号配置示例文件，只提供格式模板，不包含真实账号密码。 |
| `verify.py` | 账号验证辅助入口，通过尝试获取 Token 判断账号是否可用。 |
