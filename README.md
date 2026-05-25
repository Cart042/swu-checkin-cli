# swu-checkin

西南大学自动打卡命令行脚本。项目通过 Playwright 无头浏览器完成统一身份认证登录，使用 ddddocr 识别验证码，并支持多账号并发签到和多种消息推送。

## 文件结构

```text
.
├── check_in.py          # 主程序入口
├── get_info.py          # 登录、验证码识别、Token 获取和业务接口
├── notify.py            # 消息推送
├── verify.py            # 账号验证入口
├── des.py               # DES 相关逻辑
├── Dockerfile           # Docker 镜像构建文件
├── docker-compose.yml   # Docker Compose 配置
├── requirements.txt     # Python 依赖
├── users.json.example   # 多账号配置示例
├── .env.example         # 环境变量配置模板
└── README.md
```

## 环境要求

- Python 3.10+
- Python 依赖：`requests`、`playwright`、`ddddocr`、`python-dotenv`
- Linux 环境可能还需要 OpenCV/ONNX 和 Playwright 运行所需系统库，例如 `libgl1`、`libglib2.0-0`

安装依赖：

```bash
pip install -r requirements.txt
playwright install chromium
```

## 配置账号

脚本按以下优先级读取账号配置：

1. 命令行参数 `-u` / `-p`
2. 当前目录下的 `users.json`
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

## 先检查配置

首次部署或修改配置后，建议先运行配置检查：

```bash
python check_in.py --check-config
```

这个命令只检查本地配置和依赖，不会登录、不会打卡、不会发送推送。它会显示：

- 当前使用的账号来源和账号数量
- Token 缓存是否存在
- 已配置的推送渠道
- `requests`、`playwright`、`ddddocr`、`python-dotenv` 是否可用
- `SWU_MAX_WORKERS` 并发配置是否有效

## 运行打卡

使用配置文件或环境变量中的账号：

```bash
python check_in.py
```

临时指定一个账号：

```bash
python check_in.py -u your_username -p your_password
```

强制重新登录并忽略 Token 缓存：

```bash
python check_in.py --force-login
```

## 数字配置菜单

如果不想手动编辑 JSON 或 `.env`，可以打开交互式数字菜单：

```bash
python check_in.py -m
```

也可以使用完整参数：

```bash
python check_in.py --menu
```

菜单支持：

- 查看配置检查
- 查看 `users.json` 中的账号
- 添加账号
- 删除账号
- 修改账号密码
- 设置并发线程数
- 配置推送通道
- 查看配置文件路径
- 清除 Token 缓存
- 测试推送通道
- 立即执行一次打卡

账号密码会写入配置目录中的 `users.json`，推送配置和并发数会写入配置目录中的 `.env`。默认配置目录是脚本当前目录；Docker 中默认是 `/data`。

## Docker

镜像默认从 `/data` 读取和保存配置。建议把宿主机的 `./data` 目录挂载进去，这样 `users.json`、`.env` 和 `.token_cache.json` 都会持久保存，容器删除后也不会丢。

```bash
mkdir -p data
docker build -t swu-checkin-cli .
docker run --rm -v $(pwd)/data:/data swu-checkin-cli
```

在 Docker 中打开数字菜单：

```bash
docker run --rm -it -v $(pwd)/data:/data swu-checkin-cli python check_in.py -m
```

使用 Docker Compose：

```bash
docker compose build
docker compose run --rm swu-checkin
docker compose run --rm -it swu-checkin python check_in.py -m
```

## 定时任务

推荐在 VPS 宿主机上用 `cron` 定时调用 Docker Compose。这样容器仍然是一次性运行，配置、日志和 Token 缓存保存在宿主机目录中。

先创建日志目录：

```bash
mkdir -p data logs
```

编辑 crontab：

```bash
crontab -e
```

示例：每天 22:10 运行一次打卡，并把日志写入 `logs/checkin.log`：

```cron
10 22 * * * cd /path/to/swu-checkin/cli && docker compose run --rm swu-checkin >> logs/checkin.log 2>&1
```

如果需要先调出菜单修改账号或推送配置：

```bash
cd /path/to/swu-checkin/cli
docker compose run --rm -it swu-checkin python check_in.py -m
```

脚本内置运行锁，锁文件保存在配置目录的 `.run.lock`。如果上一次任务还没结束，下一次定时触发会自动跳过，避免重复启动浏览器或重复签到。默认超过 2 小时的锁会被视为过期锁并自动清理，可以通过 `SWU_LOCK_STALE_SECONDS` 调整。

## 推送配置

在 `.env` 或运行环境中设置对应变量即可启用推送：

- `PUSH_DINGTALK_TOKEN` / `PUSH_DINGTALK_SECRET`：钉钉机器人
- `PUSH_QYWX_KEY`：企业微信群机器人
- `PUSH_BARK_KEY` / `PUSH_BARK_URL`：Bark
- `PUSH_SERVERCHAN_KEY`：Server 酱
- `PUSH_PUSHDEER_KEY`：PushDeer

未配置任何推送渠道时，脚本只在日志中输出结果。

## 常用环境变量

- `SWU_USERNAME`：单账号用户名
- `SWU_PASSWORD`：单账号密码
- `SWU_USERS`：多账号 JSON 字符串
- `SWU_MAX_WORKERS`：最大并发线程数，默认 `3`
- `SWU_LOG_LEVEL`：日志级别，默认 `INFO`，可选 `DEBUG` / `INFO` / `WARNING` / `ERROR`
- `SWU_CONFIG_DIR`：配置目录，默认脚本当前目录；Docker 镜像中默认 `/data`
- `SWU_LOCK_STALE_SECONDS`：运行锁过期时间，默认 `7200` 秒

## 返回状态

脚本内部使用以下状态码汇总每个账号的结果：

- `0`：今日暂无签到任务
- `1`：签到成功
- `2`：今日已签到，无需重复操作
- `3`：账号或密码验证失败
- `4`：连接错误或请求超时
- `5`：请假中，请检查是否有打卡任务
- `6`：登录页加载失败或超时，可能是学校服务或网络异常
- `7`：验证码连续识别失败
- `8`：登录成功但 Token 提取失败，可能是页面结构变化
- `9`：学校登录页结构可能变化
- `10`：学校接口返回异常，可能是服务暂时不可用
- `11`：Token 校验失败或已失效，可尝试 `--force-login`

## Credits

核心打卡逻辑基于开源项目 [ptbb2005/swu-checkin](https://github.com/ptbb2005/swu-checkin)，本项目在命令行、多账号、缓存和推送体验上做了整理。
