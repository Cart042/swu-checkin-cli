# 文件备注

本文件记录仓库中每个公开文件的用途，方便部署、维护和排查问题。

| 文件 | 备注 |
| --- | --- |
| `.dockerignore` | Docker 构建忽略规则，防止本地账号配置、环境变量、Token 缓存、Git 元数据和 Python 缓存进入镜像。 |
| `.env.example` | 环境变量配置模板，包含账号、并发数、日志级别和各类推送通道示例。 |
| `.gitignore` | Git 忽略规则，防止提交 `users.json`、`.env`、`.token_cache.json` 和 Python 缓存。 |
| `Dockerfile` | Docker 镜像构建文件，安装 Python 依赖、Playwright Chromium、系统运行库，并默认使用 `/data` 保存配置。 |
| `LICENSE` | 项目开源许可证，声明 MIT License 和版权信息。 |
| `README.md` | 项目使用文档，说明安装、配置检查、数字菜单、Docker、定时任务、推送和状态码。 |
| `check_in.py` | 主程序入口，负责账号读取、配置检查、数字菜单、签到流程、运行锁、错误信息细化和推送汇总。 |
| `des.py` | DES 加解密辅助模块，保留统一身份认证相关加密逻辑。 |
| `docker-compose.yml` | Docker Compose 部署配置，适合 VPS 上挂载 `./data` 目录并运行一次性打卡任务。 |
| `get_info.py` | 登录与接口模块，负责 Playwright 登录、验证码识别、Token 获取/缓存、宿舍和今日任务接口读取。 |
| `notify.py` | 推送模块，支持钉钉、企业微信、Bark、Server 酱和 PushDeer。 |
| `requirements.txt` | Python 依赖列表，声明 `requests`、`playwright`、`ddddocr`、`python-dotenv`。 |
| `users.json.example` | 多账号配置示例文件，只提供格式模板，不包含真实账号密码。 |
| `verify.py` | 账号验证辅助入口，通过尝试获取 Token 判断账号是否可用。 |
