# 配置说明

## 登录密钥

首次默认密钥为 `moyu666`。登录后在“系统设置”中修改，文件部署会立即热生效；当前浏览器登录会话也会同步更新。

如果设置了 `CHATGPT2API_AUTH_KEY`，环境变量优先于 `config.json`，页面会将密钥设置标记为“环境变量管理”，修改环境变量后需重启服务。

## 邮箱基础配置

至少配置一种可用邮箱来源：

- Outlook 库存：导入邮箱、密码、客户端 ID、Refresh Token 等来源数据，并按用途分组。
- Gmail OAuth：配置授权方式、IMAP 地址、读取数量与 alias 规则。
- Cloudflare 域名邮箱：配置 API 地址、管理密码和接收域名。
- DuckMail / DuckGo：配置对应 API Key、Token、域名与收件接口。
- OpenAI-CPA 邮箱池：配置启用状态、域名、Webhook Secret 和域名分组策略。

密钥、Token 和密码只保存在本地运行数据中，API 返回时使用存在标记和脱敏预览。

## 代理基础配置

支持单代理、订阅节点池、外部 Mihomo 和托管 Mihomo。首次使用应完成：

1. 添加代理 URL 或订阅地址。
2. 执行连通性测试。
3. 根据注册线程数配置节点数和线程端口。
4. 启用账号节点粘性，使后续测活和 OAuth 优先复用注册节点。

不要在公开 Issue、日志或截图中提交带认证信息的代理 URL。

## 系统监控配置

- `poll_interval_seconds`：实时监控刷新间隔，范围 2-30 秒。
- `memory_warning_percent`：预测系统内存预警阈值，范围 50%-98%。
- `memory_baseline_mb`：服务基础内存估算。
- `memory_per_worker_mb`：每个注册线程的额外内存预算。

这些字段保存到 `data/system_settings.json`，保存后立即热更新。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHATGPT2API_AUTH_KEY` | 空 | 覆盖文件中的登录密钥 |
| `CHATGPT2API_PYTHON` | 自动检测 | Windows 指定 Python 3.13 路径 |
| `CHATGPT2API_HOST` | `127.0.0.1` | 服务监听地址 |
| `CHATGPT2API_PORT` | `8000` | 服务端口 |
| `CHATGPT2API_CONFIG_FILE` | `config.json` | 配置文件路径 |
| `CHATGPT2API_DATA_DIR` | `data/` | 数据目录 |
| `CHATGPT2API_LOG_DIR` | `logs/` | 日志目录 |
| `CHATGPT2API_INITIAL_AUTH_KEY` | `moyu666` | Docker 首次生成密钥 |
