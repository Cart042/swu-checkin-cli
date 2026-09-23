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
- 用 `swu-checkin -m`（或 `python check_in.py -m`）打开数字菜单，配置账号、并发和推送。
- 使用 Docker Compose 在 VPS 上运行一次性任务。
- 使用 GitHub Actions 手动运行，也支持 `schedule` 定时触发（不推荐将其用于要求准点执行的正式签到任务）。
- 支持钉钉、企业微信、Bark、Server 酱、PushDeer 和 Telegram 推送。

学校官网的网络出口和登录页策略会影响运行结果。运行主机需要能够直接访问学校官网和统一认证页面。

## 快速开始

```bash
git clone https://github.com/Cart042/swu-checkin-cli.git
cd swu-checkin-cli
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
python -m playwright install --with-deps --only-shell chromium
swu-checkin -m
```

菜单会引导你添加一个或多个账号。也可以手动复制示例文件：

```bash
cp users.json.example users.json
cp .env.example .env
```

先检查配置和依赖：

```bash
swu-checkin --check-config
```

`--check-config` 只检查本地账号、依赖、缓存和推送配置，不会访问学校网络。需要手动诊断网络时再运行：

```bash
swu-checkin --check-network
```

### 命令行入口

实现位于 `src/swu_checkin/` 包中，下面三种方式运行的是同一份代码：

| 入口 | 适用场景 |
| --- | --- |
| `swu-checkin` | 安装后（`pip install -e .` 或 `pip install .`）的正式入口。 |
| `python -m swu_checkin` | 已安装、但不想依赖源码目录里的兼容脚本时使用。 |
| `python check_in.py` | 源码检出目录里的兼容入口；Docker 镜像和已有部署脚本继续使用它。 |

`SWU_CONFIG_DIR` 未设置时，源码检出会使用仓库根目录，兼容入口与包内代码解析出的配置目录完全一致。从 wheel 安装到 `site-packages` 时默认使用当前工作目录，因此部署环境建议显式设置 `SWU_CONFIG_DIR`（Docker 镜像使用 `/data`）。

## 账号配置

程序按以下顺序读取账号：

1. 命令行 `-u` / `-p`。
2. 配置目录中的 `users.json`。
3. `SWU_USERS` 环境变量中的 JSON 数组。
4. `SWU_USERNAME` / `SWU_PASSWORD`。

这里的账号填的是**教务系统（校园网 / 统一认证）的账户名和密码**，不是学号——登录页要求的就是你在统一认证里使用的那个账户名。

账号来源按顺序使用第一个有效的非空配置；已经存在但格式错误的高优先级来源会直接报错，不会静默改用低优先级来源。`.env` 会在读取这些来源前加载，进程环境变量（例如 GitHub Actions Secrets）优先于 `.env`。

`users.json` 示例：

```json
[
  {
    "username": "你的教务系统（校园网）账户名",
    "password": "你的密码"
  },
  {
    "username": "另一个教务系统（校园网）账户名",
    "password": "另一个密码"
  }
]
```

默认配置目录是仓库根目录（wheel 安装时是当前工作目录）；Docker 中是 `/data`。账号文件、`.env`、Token 缓存和运行锁都应只保存在本地或挂载的数据目录，不要提交到仓库。

## 数字菜单

```bash
swu-checkin -m
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

> **不推荐使用 GitHub Actions 的 `schedule` 作为正式定时签到方案。** GitHub 的定时工作流不保证在 cron 指定的分钟准点启动，实际运行中经常会出现延迟，繁忙时延迟可能更明显。因此，如果你要求签到任务尽量按设定时间执行，建议在 VPS 上使用 `systemd timer` 或 `cron`；GitHub Actions 更适合作为手动触发、备用执行或测试入口。

工作流文件是 `.github/workflows/swu-check.yml`。推荐在仓库的 **Settings → Secrets and variables → Actions** 中添加一个 `SWU_USERS` Secret，用一个 JSON 数组保存一个或多个账号：

```json
[
  {"username":"account1","password":"password1"},
  {"username":"account2","password":"password2"}
]
```

其中 `username` 填教务系统（校园网 / 统一认证）的账户名，**不是学号**。工作流会把整个 `SWU_USERS` Secret 直接交给程序，程序按现有多账号逻辑并发执行签到、只重试失败账号，并在最后汇总所有账号的结果。

为兼容旧配置，工作流仍会读取 `SWU_USERNAME` 和 `SWU_PASSWORD`。如果 `SWU_USERS` 未设置，可以继续只配置这两个 Secret 运行单账号签到；如果同时配置了 `SWU_USERS` 和单账号 Secrets，程序会按账号来源优先级使用 `SWU_USERS`。

需要使用推送时，再按需添加对应 Secret：`PUSH_DINGTALK_TOKEN`、`PUSH_DINGTALK_SECRET`、`PUSH_QYWX_KEY`、`PUSH_BARK_KEY`、`PUSH_BARK_URL`、`PUSH_SERVERCHAN_KEY`、`PUSH_PUSHDEER_KEY`、`PUSH_TELEGRAM_BOT_TOKEN`、`PUSH_TELEGRAM_CHAT_ID`。

也可以使用 GitHub CLI 写入多账号 Secret：

```bash
gh secret set SWU_USERS --body '[{"username":"account1","password":"password1"},{"username":"account2","password":"password2"}]'
```

如果只需要单账号，也仍可使用旧方式：

```bash
gh secret set SWU_USERNAME --body '<教务系统（校园网）账户名>'
gh secret set SWU_PASSWORD --body '<校园网密码>'
```

如果 `SWU_USERS`、`SWU_USERNAME` 和 `SWU_PASSWORD` 都没有提供可用账号，工作流会停在配置检查并报 `[FAIL] 账号配置：未找到可用账号`。

### 开启 / 关闭每日自动打卡

每天北京时间 21:05 的定时触发由**工作流自身的启用状态**决定，直接在仓库里切换即可：

```bash
gh workflow enable  swu-check.yml   # 开启每日自动打卡
gh workflow disable swu-check.yml   # 完全关闭：定时和手动都不再触发
```

也可以在 Actions 页面左上角使用 **Enable workflow / Disable workflow**。注意两个容易踩的点：

- 这个状态保存在 GitHub 上，不在仓库里。GitHub 会在仓库长期不活跃后自动停用定时工作流（本项目就遇到过：`main` 从 6 月 30 日起没有提交，定时打卡在 8 月 29 日——正好 60 天后——被停掉），所以一段时间没收到打卡记录时，先来 Actions 页面确认工作流还是 enabled。
- **被禁用的工作流连手动触发也不可用**（GitHub 会返回 `HTTP 422: Cannot trigger a 'workflow_dispatch' on a disabled workflow`）。只想临时补一次打卡时，先 `enable`、跑完再 `disable`。

> **Fork 本项目的用户注意**：GitHub 默认不在 fork 里运行定时工作流，而且 Secrets 不会随 fork 复制过来。要让它工作必须自己动手两步：① 在 fork 的 Actions 页面点 **Enable workflow** 启用 `SWU Check-in`，否则定时任务永远不会触发；② 按上面的说明配置自己的 `SWU_USERS`，或者使用 `SWU_USERNAME` / `SWU_PASSWORD` 配置单账号。启用后每天北京时间 21:05 自动打卡，也可以用 **Run workflow** 手动跑一次。

手动触发时可以勾选 `debug`，仅在排查登录页问题时保存脱敏文本调试信息；默认不启用调试。调试 artifact 保留 3 天，文件中不应包含真实密码或验证码。

工作流使用 Python 3.11、浏览器登录和 Chromium headless shell。网络或登录页偶发异常时，优先手动重跑，再下载调试 artifact 查看页面结构变化。登录链路的已知失败特征、设备 Cookie 规则和失败原因对照见 [docs/login-troubleshooting.md](docs/login-troubleshooting.md)。

仓库另有 `PR CI` 工作流，按职责分成四个 job，任何一个失败都会阻断 PR：

| Job | 内容 | Python |
| --- | --- | --- |
| `Lint` | `ruff check` 和 `ruff format --check` | 3.11 |
| `Type check` | `mypy` | 3.11 |
| `Unit tests` | `compileall`、离线单元测试、`check_in.py --help` | 3.11 / 3.12 |
| `Runtime checks` | 安装 Chromium 与 ddddocr 后运行 `scripts/smoke_runtime.py` | 3.11 |

四个 job 之外还有一个 `CI required checks` gate job，用于把结果汇总成一个稳定的检查名，分支保护只需要锁这一个名字。前三个 job 属于 Fast CI，只安装 `requests` 和 `python-dotenv`，几秒内即可反馈；浏览器和 OCR 初始化更重，因此运行时冒烟检查单独成 job。冒烟检查在 `127.0.0.1` 启动本地页面，用无 channel 的 headless Chromium 打开页面，再初始化 ddddocr 并识别仓库内置的合成验证码图片。所有 PR 检查都不登录学校网站、不执行签到，也不需要真实账号。

仓库还有 `Runtime Smoke` 工作流，每天北京时间 03:00 定时运行同一套 Chromium + ddddocr 冒烟检查，并在完整运行时依赖下启动一次 CLI。它覆盖的是「没有 PR 时依赖或 runner 镜像悄悄漂移」这一类问题，可以手动触发。跑失败不影响 PR，但说明当天需要人工确认运行时依赖。

`main` 的分支保护（Ruleset `protect-main`）已经应用：禁止删除和 force push，合并必须走 PR 并通过 `CI required checks`。规则内容、重新应用和临时删除的命令见 [docs/branch-protection.md](docs/branch-protection.md)。

公开仓库的 PR 工作流只使用 GitHub 托管的 `ubuntu-latest`。不要把 `pull_request` 事件接到自托管 runner：fork 可以修改被检出的代码，而签到凭据和部署主机必须与 PR 构建隔离。需要自托管 runner 时请放在私有运行仓库中，并只允许受信任的定时工作流使用。

需要本地验证完整运行时依赖时执行：

```bash
python scripts/smoke_runtime.py
```

安装命令使用 Playwright 的 [Chromium headless shell](https://playwright.dev/python/docs/browsers#chromium-headless-shell) 模式。PR CI 和定时签到工作流都通过 `actions/setup-python` 按 `requirements.txt` 和 `pyproject.toml` 启用 pip 缓存；相关配置见 [setup-python 的依赖缓存说明](https://github.com/actions/setup-python#caching-packages-dependencies)。缓存只加速 Python 依赖安装，运行内存变化应以实际测量为准。

## Telegram 手动签到

先按现有 Telegram 推送配置填写 `PUSH_TELEGRAM_BOT_TOKEN` 和
`PUSH_TELEGRAM_CHAT_ID`。Controller 只接受这个 Chat ID 对应的私人聊天，
群聊成员和其他用户不能触发签到。

在与正常签到相同的环境和 `SWU_CONFIG_DIR` 下启动：

```bash
python -m swu_checkin.telegram_control
```

Controller 会启动现有的 `python -m swu_checkin` 子进程，因此 systemd 服务应复用正常签到的同一
`User`、`SWU_CONFIG_DIR`、`HOME` 和 `PLAYWRIGHT_BROWSERS_PATH`，不要另建第二套配置或浏览器缓存。
以下以推荐的生产布局为例：

```ini
[Unit]
Description=SWU Telegram manual check-in controller
After=network-online.target

[Service]
User=swu-checkin
WorkingDirectory=/opt/swu-checkin-cli
Environment=HOME=/var/lib/swu-checkin
Environment=SWU_CONFIG_DIR=/var/lib/swu-checkin
Environment=PLAYWRIGHT_BROWSERS_PATH=/var/lib/swu-checkin/ms-playwright
ExecStart=/opt/swu-checkin-cli/.venv/bin/python -m swu_checkin.telegram_control
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

如果现有 `swu-checkin.service` 使用了不同路径，直接复用其中对应的运行环境即可；
Controller 会从同一个 `SWU_CONFIG_DIR/.env` 读取 Telegram 配置，并让手动签到与定时签到共享账号、
Token 缓存和 Chromium。

一次完成服务配置并启动后，Telegram 私聊 Bot 发送 `/checkin` 即可手动签到，
无需再登录 SSH。原有 GitHub Actions 的 `workflow_dispatch` 仍可作为备用手动入口。

## Docker

Docker 镜像使用 Python 3.11 和 Playwright Chromium headless shell，并以非 root 用户（uid/gid `10001`）运行：容器内长期保存账号、Token 缓存、日志和运行锁。推荐将 `/data` 挂载到宿主机保存账号和缓存：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
docker compose build
docker compose run --rm -it swu-checkin python check_in.py -m
docker compose run --rm swu-checkin
```

绑定挂载的宿主机目录必须对容器内的 uid `10001` 可写，上面的 `chown` 就是为此。如果不想用固定 uid，可以在运行时覆盖成当前用户：

```bash
SWU_UID=$(id -u) SWU_GID=$(id -g) docker compose run --rm swu-checkin
```

也可以通过环境变量传入单个账号：

```bash
SWU_USERNAME=your_campus_account SWU_PASSWORD=your_password \
  docker compose run --rm swu-checkin
```

需要登录诊断文本时，把 `SWU_DEBUG_DIR` 指向数据目录，例如 `-e SWU_DEBUG_DIR=/data/debug`，这样诊断文件会落在挂载卷里而不是容器内的临时层。

不要把真实账号、`.env`、Token 缓存、日志、调试目录或运行锁放入镜像。`.dockerignore` 已排除这些运行文件。

## 发布

推送 `v*` 标签会触发 `.github/workflows/release.yml`：

1. 校验标签与 `pyproject.toml` 的 `project.version` 一致（不一致直接失败）；
2. 构建 sdist 与 wheel，作为 Release 附件发布；
3. 构建 Docker 镜像并推送到 GHCR：`ghcr.io/cart042/swu-checkin-cli:<version>`，正式版本额外打 `:<major>.<minor>` 和 `:latest` 标签。

发版流程：

```bash
# 1. 修改 pyproject.toml 与 src/swu_checkin/__init__.py 的版本号
# 2. 确认 CI 通过后打标签
git tag v1.2.0
git push origin v1.2.0
```

镜像只在 `linux/amd64` 上发布（目标是 VPS）；需要 arm64 时请在本机执行 `docker build -t swu-checkin-cli .`。依赖更新由 `.github/dependabot.yml` 每周检查 Python 依赖、GitHub Actions 和 Docker 基础镜像。

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
| `SWU_USERNAME` / `SWU_PASSWORD` | 教务系统（校园网 / 统一认证）的账户名和密码；账户名不是学号 |
| `SWU_USERS` | 多账号 JSON 数组 |
| `SWU_CONFIG_DIR` | 配置目录，默认脚本目录；Docker 默认 `/data` |
| `SWU_MAX_WORKERS` | 最大并发线程数，默认 `3` |
| `SWU_RETRY_INTERVAL_SECONDS` | 失败账号重试间隔，默认 `300` 秒 |
| `SWU_MAX_ROUNDS` | 失败账号最多重试轮数，默认 `3` |
| `SWU_RUN_DEADLINE_SECONDS` | 单次任务总时限，默认 `900` 秒 |
| `SWU_LOG_LEVEL` | 日志级别，默认 `INFO`；可选 `DEBUG`、`WARNING`、`ERROR` |
| `SWU_DEBUG_DIR` | 登录异常调试目录；仅排查问题时设置，例如 `debug` |
| `SWU_LOGIN_UA` | 覆盖浏览器登录使用的 User-Agent；默认按运行中浏览器版本和当前平台生成 |
| `SWU_LOGIN_ENTRY` | 登录入口；默认 `ywtb`（从一网通办进入），设为 `portal` 则从门户联邦入口开始 |
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

状态码在 `src/swu_checkin/status.py` 中以 `CheckinStatus` 枚举定义，登录失败原因
（`FailureReason`）到状态码的映射也在同一处维护；这些数值保持向后兼容，
脚本和推送内容可以直接按整数判断。

## 文件结构

```text
.
├── .github/workflows       # 定时、手动和离线 PR 检查
├── check_in.py             # 兼容入口，等价于 swu-checkin / python -m swu_checkin
├── pyproject.toml          # 项目元数据、Ruff 与 mypy 配置
├── src/swu_checkin/
│   ├── cli.py              # CLI、配置检查、运行锁和结果汇总
│   ├── config.py           # 账号来源、校验、dotenv 和本地配置文件
│   ├── menu.py             # 数字配置菜单
│   ├── status.py           # CLI 与签到服务共用的状态码文本
│   ├── checkin_service.py  # 单账号签到流程和签到业务状态
│   ├── runner.py           # 多账号并发、重试和汇总
│   ├── notify.py           # 推送渠道
│   ├── cache.py            # Token 缓存读写
│   ├── atomic_io.py        # 私有配置文件的原子写入
│   ├── logging_utils.py    # 日志初始化
│   ├── api/school.py       # 学校 HTTP 会话、请求和接口数据
│   └── auth/               # 登录链：flow / browser / captcha / cookies / tokens / pages
├── scripts/smoke_runtime.py # 本地 Chromium + ddddocr 运行时冒烟检查
├── scripts/profile_login.py # 交互式只读登录和缓存性能测量（不执行签到）
├── docs/performance.md      # 性能测量方法、限制和安全说明
├── docs/login-troubleshooting.md # 统一认证失败特征、设备 Cookie 规则与失败原因对照
├── Dockerfile              # Python 3.11 + Playwright 镜像
├── docker-compose.yml      # Docker Compose 配置
├── FILES.md                # 文件用途说明
├── requirements.txt        # Python 依赖
├── tests/fixtures/login/    # 脱敏登录页、跳转、Token 与 Cookie 样本
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

## 开发与代码检查

本地开发使用与运行环境一致的 Python 3.11 或 3.12：

```bash
python -m pip install -e ".[dev,test]"
```

提交前请至少运行以下三条命令，它们的失败会直接阻断 PR：

```bash
ruff check .
ruff format --check .
python -m unittest discover -s tests -t .
```

类型检查使用宽松设置，只保证已有代码不引入新的类型错误：

```bash
mypy
```

浏览器和验证码相关的运行时依赖不参与离线单元测试。需要完整验证本地运行时（headless Chromium + ddddocr）时再运行：

```bash
python -m playwright install --with-deps --only-shell chromium
python scripts/smoke_runtime.py
```

## Credits

核心打卡逻辑基于开源项目 [ptbb2005/swu-checkin](https://github.com/ptbb2005/swu-checkin)，本项目在浏览器登录、多账号、缓存、Docker 部署和推送体验上做了整理。
