# swu-checkin (命令行版)

一个用于校园网钉钉相关签到流程的自动化打卡脚本项目（纯命令行版本）。

由于统一身份认证系统（CAS）启用了网宿盾 UKEY / WAF 防护以及图形验证码，本项目使用 **Playwright 无头浏览器** 绕过 WAF，并集成 **ddddocr** 自动识别验证码。支持多账号并发/循环签到及各种推送通知。

## 目录结构

```text
.
├── check_in.py      # 主程序入口
├── get_info.py      # Playwright 登录、自动识别验证码及 Token 获取模块
├── des.py           # DES 加密模块
├── notify.py        # 消息推送通道模块
├── verify.py        # 令牌校验模块
├── Dockerfile       # Docker 镜像构建文件
├── requirements.txt # Python 依赖包列表
├── users.json.example # 多账号配置示例
├── .env.example     # 环境变量配置模板
└── README.md        # 命令行使用说明文档
```

## 环境要求 & 依赖安装

- Python 3.10+
- 依赖：`requests`、`playwright`、`ddddocr`、`python-dotenv`
- 系统图形库依赖：在 Linux 系统中，`ddddocr` 依赖的 OpenCV/ONNX 以及 Playwright 需要安装相应的系统库（如 `libgl1`, `libglib2.0-0`）。

### 安装方式

```bash
# 安装 Python 库依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器内核
playwright install chromium
```

## 账号配置方式

脚本支持以下方式配置账号和环境变量（优先级从高到低）：

### 1. 配置文件 `users.json`（推荐）
在同目录下创建 `users.json` 文件：
```json
[
  {
    "username": "你的校园网账号1",
    "password": "你的密码1"
  },
  {
    "username": "你的校园网账号2",
    "password": "你的密码2"
  }
]
```

### 2. 环境变量配置文件 `.env`（推荐，支持单账号/多账号/推送配置）
在 `cli/` 目录下创建 `.env` 文件（可拷贝 `.env.example` 进行修改）：
```text
SWU_USERNAME=你的校园网账号
SWU_PASSWORD=你的密码
SWU_LOG_LEVEL=INFO

# 消息推送配置
PUSH_DINGTALK_TOKEN=...
```

### 3. 环境变量 `SWU_USERS`（JSON 字符串格式，适合 Docker / 容器环境）
```bash
export SWU_USERS='[{"username": "校园网账号1", "password": "密码1"}, {"username": "校园网账号2", "password": "密码2"}]'
```

### 4. 单账号环境变量 `SWU_USERNAME` / `SWU_PASSWORD`
```bash
export SWU_USERNAME="你的校园网账号"
export SWU_PASSWORD="你的密码"
```

## 运行方式

### 1. 本地 / VPS 直接打卡
```bash
python check_in.py
```

### 2. 使用 Docker 部署
```bash
# 构建镜像
docker build -t swu-checkin-cli .

# 运行单次打卡容器
docker run --rm -v $(pwd)/users.json:/app/users.json swu-checkin-cli
```

## 消息推送配置

项目支持通过环境变量配置消息推送通道，打卡完成后会将执行汇总结果推送到指定的设备。

支持以下推送方式（配置对应的环境变量即可激活）：

### 1. 钉钉机器人 (DingTalk Bot)
- **`PUSH_DINGTALK_TOKEN`**: 机器人的 `access_token`
- **`PUSH_DINGTALK_SECRET`**: (可选) 机器人的签名密钥，用于加签安全设置

### 2. 企业微信群机器人 (WeChat Work Robot)
- **`PUSH_QYWX_KEY`**: 机器人的 `webhook` 链接中的 `key` 参数

### 3. Bark (iOS 消息推送)
- **`PUSH_BARK_KEY`**: 你的 Bark `device_key`
- **`PUSH_BARK_URL`**: (可选) 自建 Bark 服务器的主机地址，默认为官方服务 `https://api.day.app`

### 4. Server酱 (微信通知)
- **`PUSH_SERVERCHAN_KEY`**: Server酱·SendKey

### 5. PushDeer
- **`PUSH_PUSHDEER_KEY`**: PushDeer 的 `PushKey`

## 日志系统配置

项目内置了标准化的日志输出（使用标准库 `logging`），支持通过环境变量控制输出详细度：

- **`SWU_LOG_LEVEL`**：控制日志打印级别（可选，默认为 `INFO`。可选值包括 `DEBUG`、`INFO`、`WARNING`、`ERROR`）。
  - `DEBUG`：输出详细的调试日志，包括 Playwright 自动登录的所有细节，适合在本地调试或排查问题时开启。
  - `INFO`：常规流程日志，记录账号打卡的整体流转和结果。

### 终端编码安全机制
脚本内置了安全日志处理器。当检测到控制台（如 Windows 的 GBK 编码终端）无法正确显示 `✅` / `❌` 等 emoji 字符时，会自动降级为 ASCII 字符（`[OK]` / `[FAIL]`）输出，彻底杜绝了因终端编码不支持而引发的崩溃。

## 返回状态说明

`check_in.py` 执行后对每个账号输出其结果：
- `0`：今日暂无签到任务
- `1`：签到成功
- `2`：今日已签到，无需重复操作
- `3`：账号密码验证失败
- `4`：连接错误或请求超时
- `5`：请假中，请检查是否有打卡任务

## 致谢 / Credits
本项目由 [Cart042](https://github.com/Cart042) 进行维护与二次开发。核心打卡逻辑与代码基于原作者 [ptbb2005/swu-checkin](https://github.com/ptbb2005/swu-checkin) 
开源项目，感谢原作者的开源贡献。
