<div align="center">

# swu-checkin-cli

西南大学自动打卡命令行工具，使用浏览器登录，支持多账号、数字菜单、Docker、GitHub Actions 和多通道推送。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![License](https://img.shields.io/github/license/Cart042/swu-checkin-cli)](LICENSE)

</div>

## 能力

- 浏览器登录学校统一认证页面，自动识别登录页验证码并获取 Token。
- 使用 `users.json` 或环境变量管理多个账号。
- 用 `python check_in.py -m` 打开数字菜单，配置账号、并发和推送。
- 使用 Docker Compose 在 VPS 上运行一次性任务。
- 使用 GitHub Actions 在每天北京时间 21:05（UTC 13:05）定时运行，也可以手动运行。
- 支持钉钉、企业微信、Bark、Server 酱、PushDeer 和 Telegram 推送。

学校官网的网络出口和登录页策略会影响运行结果。运行主机需要能够直接访问学校官网和统一认证页面。

## 快速开始

```bash
git clone https://github.com/Cart042/swu-checkin-cli.git
cd swu-checkin-cli
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps --only-shell chromium
python check_in.py -m
```

菜单会引导你添加一个或多个账号。也可以手动复制示例文件：

```bash
cp users.json.example users.json
cp .env.example .env
```

先检查配置和依赖：

```bash
python check_in.py --check-config
```

`--check-config` 只检查本地账号、依赖、缓存和推送配置，不会访问学校网络。需要手动诊断网络时再运行：

```bash
python check_in.py --check-network
```

## 账号配置

程序按以下顺序读取账号：

1. 命令行 `-u` / `-p`。
2. 配置目录中的 `users.json`。
3. `SWU_USERS` 环境变量中的 JSON 数组。
4. `SWU_USERNAME` / `SWU_PASSWORD`。

账号来源按顺序使用第一个有效的非空配置；已经存在但格式错误的高优先级来源会直接报错，不会静默改用低优先级来源。`.env` 会在读取这些来源前加载，进程环境变量（例如 GitHub Actions Secrets）优先于 `.env`。

`users.json` 示例：

```json
[
  {
    "username": "你的校园网账号",
    "password": "你的密码"
  },
  {
    "username": "另一个校园网账号",
    "password": "另一个密码"
  }
]
```

默认配置目录是脚本所在目录；Docker 中是 `/data`。账号文件、`.env`、Token 缓存和运行锁都应只保存在本地或挂载的数据目录，不要提交到仓库。

## 数字菜单

```bash
python check_in.py -m
```

菜单可以：

- 查看配置和依赖检查结果；
- 查看、添加、删除账号和修改密码；
- 设置最大并发数；
- 配置和测试推送通道；
- 查看配置路径、清除 Token 缓存；
- 立即运行一次打卡。

菜单写入的账号文件和 `.env` 位于当前配置目录。账号列表和日志中的账号标识会尽量使用掩码显示。

## GitHub Actions

工作流文件是 `.github/workflows/swu-check.yml`。运行前，在仓库的 **Settings → Secrets and variables → Actions** 中添加：

- `SWU_USERNAME`
- `SWU_PASSWORD`
- 需要使用的推送渠道 Secret：`PUSH_DINGTALK_TOKEN`、`PUSH_DINGTALK_SECRET`、`PUSH_QYWX_KEY`、`PUSH_BARK_KEY`、`PUSH_BARK_URL`、`PUSH_SERVERCHAN_KEY`、`PUSH_PUSHDEER_KEY`
- `PUSH_TELEGRAM_BOT_TOKEN`（可选）
- `PUSH_TELEGRAM_CHAT_ID`（可选）

工作流保留每天北京时间 21:05 的定时触发、手动触发和并发保护。手动触发时可以勾选 `debug`，仅在排查登录页问题时保存脱敏文本调试信息；默认不启用调试。调试 artifact 保留 3 天，文件中不应包含真实密码或验证码。

工作流使用 Python 3.11、浏览器登录和 Chromium headless shell。网络或登录页偶发异常时，优先手动重跑，再下载调试 artifact 查看页面结构变化。

仓库另有 `Runtime PR CI` 工作流。它安装完整 `requirements.txt`，执行 Python 源码编译、离线单元测试、命令行帮助检查和运行时冒烟检查，不登录学校网站、不执行签到，也不需要真实账号。冒烟检查在 `127.0.0.1` 启动本地页面，用无 channel 的 headless Chromium 打开页面，再初始化 ddddocr 并识别仓库内置的合成验证码图片。浏览器和 OCR 初始化需要更多资源，因此该命令单独放在普通单元测试之外。

需要本地验证完整运行时依赖时执行：

```bash
python scripts/smoke_runtime.py
```

安装命令使用 Playwright 的 [Chromium headless shell](https://playwright.dev/python/docs/browsers#chromium-headless-shell) 模式。PR CI 和定时签到工作流都通过 `actions/setup-python` 按 `requirements.txt` 启用 pip 缓存；相关配置见 [setup-python 的依赖缓存说明](https://github.com/actions/setup-python#caching-packages-dependencies)。缓存只加速 Python 依赖安装，运行内存变化应以实际测量为准。

## Docker

Docker 镜像使用 Python 3.11 和 Playwright Chromium headless shell。推荐将 `/data` 挂载到宿主机保存账号和缓存：

```bash
mkdir -p data
docker compose build
docker compose run --rm -it swu-checkin python check_in.py -m
docker compose run --rm swu-checkin
```

也可以通过环境变量传入单个账号：

```bash
SWU_USERNAME=your_username SWU_PASSWORD=your_password \
  docker compose run --rm swu-checkin
```

不要把真实账号、`.env`、Token 缓存、日志、调试目录或运行锁放入镜像。`.dockerignore` 已排除这些运行文件。

## 网络

学校接口请求和浏览器登录使用直连网络，并显式忽略运行环境中的代理变量。正常签到直接执行登录和接口请求，不会先做重复的官网预检。`--check-network` 是显式的网络诊断选项；`--check-config` 保持纯本地检查。

## 推送

在 `.env` 或运行环境中按需填写：

| 渠道 | 环境变量 |
| --- | --- |
| 钉钉机器人 | `PUSH_DINGTALK_TOKEN` / `PUSH_DINGTALK_SECRET` |
| 企业微信群机器人 | `PUSH_QYWX_KEY` |
| Bark | `PUSH_BARK_KEY` / `PUSH_BARK_URL` |
| Server 酱 | `PUSH_SERVERCHAN_KEY` |
| PushDeer | `PUSH_PUSHDEER_KEY` |
| Telegram Bot | `PUSH_TELEGRAM_BOT_TOKEN` / `PUSH_TELEGRAM_CHAT_ID` |

Telegram 需要同时填写 Bot Token 和 Chat ID；菜单支持设置、修改和清除。未配置推送时，结果仍会写入日志。

## 常用环境变量

| 变量 | 说明 |
| --- | --- |
| `SWU_USERNAME` / `SWU_PASSWORD` | 单账号用户名和密码 |
| `SWU_USERS` | 多账号 JSON 数组 |
| `SWU_CONFIG_DIR` | 配置目录，默认脚本目录；Docker 默认 `/data` |
| `SWU_MAX_WORKERS` | 最大并发线程数，默认 `3` |
| `SWU_RETRY_INTERVAL_SECONDS` | 失败账号重试间隔，默认 `300` 秒 |
| `SWU_MAX_ROUNDS` | 失败账号最多重试轮数，默认 `3` |
| `SWU_RUN_DEADLINE_SECONDS` | 单次任务总时限，默认 `900` 秒 |
| `SWU_LOG_LEVEL` | 日志级别，默认 `INFO`；可选 `DEBUG`、`WARNING`、`ERROR` |
| `SWU_DEBUG_DIR` | 登录异常调试目录；仅排查问题时设置，例如 `debug` |
| `SWU_PUSH_DEADLINE_SECONDS` | 本次推送共享总预算，默认 `60` 秒，允许范围 `1-3600` 秒 |
| `PUSH_TELEGRAM_BOT_TOKEN` | Telegram Bot Token；需与 Chat ID 同时设置 |
| `PUSH_TELEGRAM_CHAT_ID` | Telegram 接收消息的 Chat ID；需与 Bot Token 同时设置 |

失败账号的重试预算默认是 900 秒，重试轮数默认 3 轮；每次网络和浏览器操作都会按剩余预算设置超时。可通过 `SWU_RUN_DEADLINE_SECONDS` 调整预算（允许范围由程序校验）。

推送通道共享独立的协作预算，默认 60 秒；可通过 `SWU_PUSH_DEADLINE_SECONDS` 调整，非法值会回退到 60 秒。

## 状态码

| 状态码 | 含义 |
| --- | --- |
| `0` | 今日暂无签到任务 |
| `1` | 签到成功 |
| `2` | 今日已签到 |
| `3` | 账号或密码验证失败 |
| `4` | 连接错误或请求超时 |
| `5` | 请假中或没有打卡任务 |
| `6` | 登录页加载失败或超时 |
| `7` | 验证码连续识别失败 |
| `8` | 登录成功但 Token 提取失败 |
| `9` | 登录页结构可能变化 |
| `10` | 学校接口返回异常 |
| `11` | Token 校验失败或已失效 |

## 文件结构

```text
.
├── .github/workflows       # 定时、手动和离线 PR 检查
├── check_in.py             # CLI、配置检查、运行锁和结果汇总
├── checkin_service.py      # 单账号签到流程和签到业务状态
├── status.py               # CLI 与签到服务共用的状态码文本
├── config.py               # 账号来源、校验、dotenv 和本地配置文件
├── menu.py                 # 数字配置菜单
├── get_info.py             # 浏览器登录、验证码和 Token
├── login.py                # 延迟加载浏览器登录依赖
├── cache.py                # Token 缓存读写
├── school_api.py           # 学校 HTTP 会话、请求和接口数据
├── notify.py               # 推送渠道
├── runner.py               # 多账号并发、重试和汇总
├── scripts/smoke_runtime.py # 本地 Chromium + ddddocr 运行时冒烟检查
├── scripts/profile_login.py # 交互式只读登录和缓存性能测量（不执行签到）
├── docs/performance.md      # 性能测量方法、限制和安全说明
├── Dockerfile              # Python 3.11 + Playwright 镜像
├── docker-compose.yml      # Docker Compose 配置
├── FILES.md                # 文件用途说明
├── requirements.txt        # Python 依赖
├── tests/test_config.py     # 账号优先级、dotenv 和配置错误测试
├── tests/test_menu.py       # 菜单文件操作测试
├── users.json.example      # 多账号示例
├── .env.example            # 环境变量模板
└── README.md
```

更多文件说明见 [FILES.md](FILES.md)。

## 安全

- 只通过本地 `.env`、`users.json` 或 GitHub Secrets 保存真实凭据。
- 不要提交 `.env`、`users.json`、`.token_cache.json`、`data/`、`logs/`、`debug/` 或 `.run.lock`。
- 如果怀疑账号或 Token 泄露，请先修改校园网密码，再删除 Token 缓存并使用 `--force-login` 重新登录。

## Credits

核心打卡逻辑基于开源项目 [ptbb2005/swu-checkin](https://github.com/ptbb2005/swu-checkin)，本项目在浏览器登录、多账号、缓存、Docker 部署和推送体验上做了整理。
