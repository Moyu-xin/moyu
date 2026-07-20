# openai-paypal

PayPal Billing Agreement 流程自动化工具，提供命令行运行方式和本地 Web UI。项目支持代理、浏览器运行时指纹、DataDome/MTR 风控信号、手动手机号 OTP，以及可选的 SMSBower 自动接码。

## 环境要求

- Python 3.10+
- pip / venv
- 可选：Playwright Chromium，用于 `headless` 运行时
- 可选：RoxyBrowser Local API，用于 `roxy` 运行时
- 可选：SMSBower API Key，用于自动接收短信验证码

## 安装

```bash
git clone https://github.com/NoneWhite1/openai-paypal.git
cd openai-paypal

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

如果使用本机 headless Chromium：

```bash
pip install -r requirements-headless.txt
python -m playwright install chromium
```

## 配置

复制示例配置文件：

```bash
cp .env.example .env
```

按需修改 `.env`：

```env
PAYPAL_PROXY_ENABLED=1
PAYPAL_PROXY_URL=http://user:pass@host:port

PAYPAL_FINGERPRINT_SOURCE=headless
PAYPAL_DATADOME_MODE=headless
PAYPAL_MTR_RUNTIME=headless
PAYPAL_RISK_SIGNALS_MODE=headless

SMSBOWER_ENABLED=0
SMSBOWER_API_KEY=replace_with_smsbower_api_key
```

常用运行时：

- `headless`：使用本机 Playwright Chromium，推荐默认方式。
- `roxy`：使用 RoxyBrowser Local API，需要配置 `PAYPAL_ROXY_API_KEY`、`PAYPAL_ROXY_API_HOST`、`PAYPAL_ROXY_API_PORT`。
- `protocol` / `python_generated`：协议或 Python 模板模式，适合轻量测试。
- `auto`：自动优先选择可用浏览器运行时。

`.env`、`cache/`、`var/`、`debug/`、`captures/` 默认已在 `.gitignore` 中忽略，不要提交真实密钥、代理账号、验证码或抓包数据。

## 命令行使用

手动手机号模式：

```bash
python main.py \
  --ba-token BA-xxxxxxxxxxxxxxxx \
  --phone +5591999999999 \
  --fingerprint-source headless \
  --datadome-mode headless \
  --mtr-runtime headless \
  --risk-signals-mode headless
```

使用 SMSBower 自动接码：

```bash
python main.py \
  --ba-token BA-xxxxxxxxxxxxxxxx \
  --smsbower \
  --smsbower-api-key YOUR_SMSBOWER_API_KEY
```

使用指定代理：

```bash
python main.py \
  --ba-token BA-xxxxxxxxxxxxxxxx \
  --phone +5591999999999 \
  --proxy \
  --proxy-url http://user:pass@host:port
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--ba-token` | 必填，Billing Agreement token，例如 `BA-...` |
| `--phone` | 手动接码手机号，带国家区号 |
| `--smsbower` | 启用 SMSBower 自动接码 |
| `--proxy` / `--no-proxy` | 启用或禁用代理 |
| `--proxy-url` | 当前运行使用的代理地址 |
| `--debug` | 输出调试日志 |
| `--max-card-attempts` | 卡信息失败后的最大重试次数 |
| `--max-flow-attempts` | 完整流程最大重试次数 |
| `--fingerprint-source` | 指纹来源：`headless`、`roxy`、`random`、`auto` 等 |
| `--datadome-mode` | DataDome 模式：`headless`、`roxy`、`protocol`、`auto`、`off` |
| `--mtr-runtime` | MTR 运行时：`headless`、`roxy`、`python_generated`、`auto`、`off` |
| `--risk-signals-mode` | Signup risk 信号模式：`headless`、`roxy`、`protocol`、`auto`、`off` |

运行结束后程序会输出 JSON 结果；成功时退出码为 `0`，失败时退出码为 `1`。

## Web UI 使用

启动本地 Web 服务：

```bash
python web.py --host 127.0.0.1 --port 8080
```

浏览器打开：

```text
http://localhost:8080
```

Web UI 支持输入 BA Token、手机号、代理、运行时模式等配置，并在需要短信验证码时等待用户输入 OTP。

生产环境建议：

```env
PAYPAL_WEB_PRODUCTION=1
PAYPAL_WEB_COOKIE_SECURE=1
PAYPAL_WEB_ALLOW_DEBUG_LOGS=0
PAYPAL_WEB_MAX_ACTIVE_JOBS=4
PAYPAL_WEB_MAX_ACTIVE_JOBS_PER_DEVICE=2
```

如果通过公网访问 Web UI，建议放在 HTTPS 反向代理后面运行。

## RoxyBrowser 模式

`.env` 示例：

```env
PAYPAL_FINGERPRINT_SOURCE=roxy
PAYPAL_DATADOME_MODE=roxy
PAYPAL_MTR_RUNTIME=roxy
PAYPAL_RISK_SIGNALS_MODE=roxy

PAYPAL_ROXY_API_KEY=replace_with_roxy_api_key
PAYPAL_ROXY_API_HOST=127.0.0.1
PAYPAL_ROXY_API_PORT=50000
PAYPAL_ROXY_HEADLESS=1
```

如果本地 Roxy workspace 无法自动发现，可以固定：

```env
PAYPAL_ROXY_WORKSPACE_ID=123456
PAYPAL_ROXY_PROJECT_ID=123456
```

## 目录结构

```text
.
├── main.py                    # 命令行入口
├── web.py                     # 本地 Web UI 入口
├── config.py                  # 默认运行配置
├── paypal/                    # 核心流程、会话、风控信号、代理、模型等模块
├── web_static/                # Web UI 静态资源
├── tools/                     # 浏览器辅助脚本
├── requirements.txt           # 基础依赖
├── requirements-headless.txt  # Playwright/headless 依赖
└── .env.example               # 环境变量示例
```

## 常见问题

### 1. Playwright 找不到 Chromium

执行：

```bash
python -m playwright install chromium
```

### 2. 代理不生效

确认 `.env` 或命令行里已启用代理：

```env
PAYPAL_PROXY_ENABLED=1
PAYPAL_PROXY_URL=http://user:pass@host:port
```

也可以运行时指定：

```bash
python main.py --ba-token BA-xxx --phone +5591999999999 --proxy --proxy-url http://user:pass@host:port
```

### 3. Web UI 看不到 debug 日志

默认会隐藏敏感 debug 日志。如需排查问题：

```env
PAYPAL_WEB_ALLOW_DEBUG_LOGS=1
```

### 4. 不想生成调试文件或抓包文件

保持以下配置为关闭状态：

```env
PAYPAL_HEADLESS_DEBUG=0
PAYPAL_HEADLESS_OPTIMIZED_DEBUG=0
PAYPAL_TRAFFIC_RECORD=0
```
