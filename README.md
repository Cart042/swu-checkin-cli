<div align="center">

# swu-checkin-cli

西南大学自动打卡命令行工具，支持多账号、Docker 部署、定时任务、数字菜单和多通道机器人推送。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![License](https://img.shields.io/github/license/Cart042/swu-checkin-cli)](LICENSE)
[![Repo](https://img.shields.io/badge/GitHub-Cart042%2Fswu--checkin--cli-181717?logo=github)](https://github.com/Cart042/swu-checkin-cli)

`python check_in.py -m` 打开数字菜单，按数字即可添加账号、配置推送、检查状态或立即运行。

</div>

## 部署位置提醒

推荐把脚本部署在中国大陆内的服务器或本地网络环境中。学校官网登录和打卡接口可能限制海外网络访问，海外 VPS 直接运行时很可能无法访问学校官网，表现为登录页加载超时、连接失败或接口请求异常。

如果必须部署在海外服务器上，需要有一个能访问学校官网的中国大陆网络出口。脚本会自动尝试直连学校官网，也会自动读取服务器已有的 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY` 环境变量；如果这些都没有，且学校官网无法直连，脚本无法凭空访问学校官网。

## 亮点

| 能力 | 说明 |
| --- | --- |
| 多账号签到 | 支持 `users.json`、环境变量和命令行临时账号，适合个人或少量账号集中管理。 |
| 交互式菜单 | SSH 中输入 `python check_in.py -m` 即可通过数字菜单配置账号、推送、并发、缓存和学校官网代理。 |
| Docker 友好 | 默认使用 `/data` 保存配置、Token 缓存和运行锁，容器删除后数据不丢。 |
| 双登录链路 | 支持历史纯 HTTP 登录和浏览器登录；原始项目已说明历史登录接口不可用，当前以浏览器链路为主。 |
| 定时任务稳妥 | 内置运行锁，避免 cron 重叠触发导致重复启动浏览器或重复签到。 |
| 推送结果 | 支持钉钉、企业微信、Bark、Server 酱和 PushDeer。 |
| 错误更清楚 | 对登录失败、验证码失败、接口异常、Token 失效等情况给出更细状态。 |

## 快速开始

```bash
git clone https://github.com/Cart042/swu-checkin-cli.git
cd swu-checkin-cli
pip install -r requirements.txt
playwright install chromium
python check_in.py -m
```

如果你在 VPS 上部署，推荐优先使用 Docker Compose：

```bash
git clone https://github.com/Cart042/swu-checkin-cli.git
cd swu-checkin-cli
mkdir -p data logs
docker compose build
docker compose run --rm -it swu-checkin python check_in.py -m
```

VPS 建议选择中国大陆内的机房或能稳定访问学校官网的网络。如果使用海外 VPS，需要服务器本身能直连学校官网，或已有可信的中国大陆代理出口。不要把 GitHub Actions 当作定时运行环境，它通常位于海外网络，且不适合保存长期账号配置。

## 命令速查

| 场景 | 命令 |
| --- | --- |
| 打开数字菜单 | `python check_in.py -m` |
| 检查配置和依赖 | `python check_in.py --check-config` |
| 执行一次签到 | `python check_in.py` |
| 临时指定账号 | `python check_in.py -u your_username -p your_password` |
| 强制重新登录 | `python check_in.py --force-login` |
| Docker 打开菜单 | `docker compose run --rm -it swu-checkin python check_in.py -m` |
| Docker 执行一次 | `docker compose run --rm swu-checkin` |

## 文件结构

```text
.
├── .github/workflows    # GitHub Actions 手动运行配置
├── check_in.py          # 主程序入口：签到流程、菜单、配置检查、运行锁和推送汇总
├── get_info.py          # 登录、验证码识别、Token 获取和学校接口
├── notify.py            # 钉钉、企业微信、Bark、Server 酱、PushDeer 推送
├── verify.py            # 账号验证入口
├── des.py               # DES 相关逻辑
├── Dockerfile           # Docker 镜像构建文件
├── docker-compose.yml   # Docker Compose 配置
├── FILES.md             # 仓库文件用途备注
├── requirements.txt     # Python 依赖
├── users.json.example   # 多账号配置示例
├── .env.example         # 环境变量配置模板
└── README.md
```

更多文件说明见 [FILES.md](FILES.md)。

## 配置账号

脚本按以下优先级读取账号配置：

1. 命令行参数 `-u` / `-p`
2. 当前配置目录下的 `users.json`
3. 环境变量 `SWU_USERS`
4. 环境变量 `SWU_USERNAME` / `SWU_PASSWORD`

推荐复制 `users.json.example` 为 `users.json`：

```json
[
  {
    "username": "你的校园网账号",
    "password": "你的密码"
  }
]
```

也可以复制 `.env.example` 为 `.env`，用环境变量配置账号、并发数和推送渠道。

## 数字菜单

不想手动编辑 JSON 或 `.env` 时，直接打开交互式菜单：

```bash
python check_in.py -m
```

菜单支持：

- 查看配置检查
- 查看、添加、删除账号
- 修改账号密码
- 设置并发线程数
- 配置和测试推送通道
- 配置学校官网代理
- 查看配置文件路径
- 清除 Token 缓存
- 立即执行一次打卡

账号密码会写入配置目录中的 `users.json`，推送配置和并发数会写入配置目录中的 `.env`。默认配置目录是脚本当前目录；Docker 中默认是 `/data`。

## 海外服务器运行方式

海外服务器能否运行取决于两件事：服务器是否能访问学校官网，以及当前学校登录页结构是否能被浏览器链路识别。

登录链路默认使用 `SWU_LOGIN_METHOD=auto`：

1. 先尝试历史纯 HTTP 登录。这条链路来自原始项目思路，不启动浏览器、不识别验证码，但原始项目已说明该登录接口不可用。
2. 如果纯 HTTP 登录失败，再回退到 Playwright 浏览器登录。

如果你要在 GitHub Actions 中排查当前页面结构，建议使用：

```bash
SWU_LOGIN_METHOD=browser
SWU_DEBUG_DIR=debug
```

网络路径按以下顺序处理：

1. 如果配置了 `SWU_PROXY_URL`，优先通过它访问学校官网。
2. 未配置 `SWU_PROXY_URL` 时，自动读取服务器已有的标准代理变量：`HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY`。
3. 如果没有任何代理配置，则直接访问学校官网。

如果服务器无法直连学校官网，也没有任何可用代理或隧道，脚本无法凭空绕过网络限制。出于账号安全考虑，本项目不会内置公共代理池。

如需手动指定可信代理，可配置：

```bash
SWU_PROXY_URL=http://proxy.example.com:7890
SWU_PROXY_USERNAME=
SWU_PROXY_PASSWORD=
```

也可以通过数字菜单配置：

```bash
python check_in.py -m
```

进入菜单后选择“配置学校官网代理”。Docker Compose 部署时，代理配置会写入挂载的 `./data/.env`，后续定时任务会自动读取。

说明：

- `SWU_PROXY_URL` 优先级高于服务器已有的 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`。
- `SWU_PROXY_URL` 支持 `http://`、`https://`、`socks4://`、`socks5://` 形式；省略协议时按 `http://` 处理。
- `SWU_PROXY_USERNAME` / `SWU_PROXY_PASSWORD` 仅在代理需要认证时填写。
- 标准代理环境变量和 `SWU_PROXY_URL` 都只用于学校官网登录和学校接口请求；推送通道仍按各平台网络情况直接访问。
- 完全不提供中国大陆服务器、代理、隧道或可直连学校官网的网络时，海外服务器无法完成登录和打卡。
- GitHub Actions 不推荐用于本项目。即使配置代理，也不适合在 GitHub Actions 中长期保存校园账号、密码、Token 或机器人 Key。

## GitHub Actions 手动运行

仓库提供了手动触发的 workflow：`.github/workflows/swu-check.yml`。它默认使用 `SWU_LOGIN_METHOD=browser`，并在登录页元素识别失败时上传 `login-debug` artifact，里面包含截图和 HTML，便于定位 GitHub Actions 上实际打开的页面。

使用前需要在 GitHub 仓库 `Settings` -> `Secrets and variables` -> `Actions` 中添加：

- `SWU_USERNAME`
- `SWU_PASSWORD`

然后进入 `Actions` -> `swu-check manual run` -> `Run workflow` 手动触发。登录方式建议先选 `browser`。如果失败，请在运行详情里下载 `login-debug` artifact，再根据截图和 HTML 判断是否是页面结构变化、验证码策略变化或账号认证异常。

不建议默认开启定时 schedule。GitHub Actions 的运行环境和出口网络可能变化，也不适合长期保存校园账号、密码和 Token。

## Docker 部署

镜像默认从 `/data` 读取和保存配置。建议把宿主机的 `./data` 目录挂载进去，这样 `users.json`、`.env`、`.token_cache.json` 和 `.run.lock` 都会持久保存。

```bash
mkdir -p data
docker compose build
docker compose run --rm swu-checkin
```

在 Docker 中打开数字菜单：

```bash
docker compose run --rm -it swu-checkin python check_in.py -m
```

## 定时任务

推荐在 VPS 宿主机上用 `cron` 定时调用 Docker Compose。容器保持一次性运行，配置、日志和 Token 缓存保存在宿主机目录中。

定时任务建议部署在中国大陆内服务器上。海外服务器需要确认能直连学校官网，或已有可信中国大陆代理出口后再运行。GitHub Actions 不推荐用于本项目，因为它通常位于海外网络，且不适合保存长期账号、密码和 Token。

```bash
mkdir -p data logs
crontab -e
```

示例：每天 22:10 运行一次，并把日志写入 `logs/checkin.log`：

```cron
10 22 * * * cd /path/to/swu-checkin-cli && docker compose run --rm swu-checkin >> logs/checkin.log 2>&1
```

脚本内置运行锁，锁文件保存在配置目录的 `.run.lock`。如果上一次任务还没结束，下一次定时触发会自动跳过。默认超过 2 小时的锁会被视为过期锁并自动清理，可以通过 `SWU_LOCK_STALE_SECONDS` 调整。

## 推送配置

在 `.env` 或运行环境中设置对应变量即可启用推送：

| 渠道 | 环境变量 |
| --- | --- |
| 钉钉机器人 | `PUSH_DINGTALK_TOKEN` / `PUSH_DINGTALK_SECRET` |
| 企业微信群机器人 | `PUSH_QYWX_KEY` |
| Bark | `PUSH_BARK_KEY` / `PUSH_BARK_URL` |
| Server 酱 | `PUSH_SERVERCHAN_KEY` |
| PushDeer | `PUSH_PUSHDEER_KEY` |

未配置任何推送渠道时，脚本只在日志中输出结果。

## 常用环境变量

| 变量 | 说明 |
| --- | --- |
| `SWU_USERNAME` | 单账号用户名 |
| `SWU_PASSWORD` | 单账号密码 |
| `SWU_USERS` | 多账号 JSON 字符串 |
| `SWU_MAX_WORKERS` | 最大并发线程数，默认 `3` |
| `SWU_LOG_LEVEL` | 日志级别，默认 `INFO`，可选 `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SWU_LOGIN_METHOD` | 登录方式，默认 `auto`；可选 `direct` 纯 HTTP 登录、`browser` 浏览器登录 |
| `SWU_CONFIG_DIR` | 配置目录，默认脚本当前目录；Docker 镜像中默认 `/data` |
| `SWU_LOCK_STALE_SECONDS` | 运行锁过期时间，默认 `7200` 秒 |
| `SWU_PROXY_URL` | 学校官网代理地址，海外 VPS 运行时可填写中国大陆代理出口 |
| `SWU_PROXY_USERNAME` | 学校官网代理用户名，无认证可留空 |
| `SWU_PROXY_PASSWORD` | 学校官网代理密码，无认证可留空 |
| `SWU_PROXY_MODE` | 代理模式，默认 `auto`；设为 `manual` 时只读取 `SWU_PROXY_URL`，设为 `off` 时禁用代理 |
| `SWU_DEBUG_DIR` | 登录调试快照目录，设置后在浏览器登录页面元素识别失败时保存截图和 HTML |

## 返回状态

| 状态码 | 含义 |
| --- | --- |
| `0` | 今日暂无签到任务 |
| `1` | 签到成功 |
| `2` | 今日已签到，无需重复操作 |
| `3` | 账号或密码验证失败 |
| `4` | 连接错误或请求超时 |
| `5` | 请假中，请检查是否有打卡任务 |
| `6` | 登录页加载失败或超时，可能是学校服务或网络异常 |
| `7` | 验证码连续识别失败 |
| `8` | 登录成功但 Token 提取失败，可能是页面结构变化 |
| `9` | 学校登录页结构可能变化 |
| `10` | 学校接口返回异常，可能是服务暂时不可用 |
| `11` | Token 校验失败或已失效，可尝试 `--force-login` |

## 安全提示

- 不要提交真实的 `users.json`、`.env`、`.token_cache.json`。
- VPS 上建议限制项目目录权限，只让当前部署用户可读写。
- 如果怀疑账号或 Token 泄露，请先修改校园网密码，再删除 `.token_cache.json` 并使用 `--force-login` 重新登录。

## Credits

核心打卡逻辑基于开源项目 [ptbb2005/swu-checkin](https://github.com/ptbb2005/swu-checkin)，本项目在命令行、多账号、缓存、Docker 部署和推送体验上做了整理。
