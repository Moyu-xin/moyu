# 部署指南

## Windows 本地部署

安装 Python 3.13、Node.js 20.9+ 和 Corepack 后，在项目根目录执行：

```powershell
.\start_local.ps1
```

启动脚本会在缺少 `config.json`、`.venv` 或 `web_dist` 时自动调用 `setup_local.ps1`。首次安装完成后访问 `http://127.0.0.1:8000/register/`，默认密钥为 `moyu666`。

常用命令：

```powershell
.\setup_local.ps1          # 手动重新安装依赖并构建
.\build_frontend.ps1       # 仅重建前端
.\start_local.ps1          # 启动
.\stop_local.ps1           # 停止
```

## Docker Compose 服务器部署

```bash
git clone <your-repository-url> moyu
cd moyu
sh deploy_server.sh
```

默认配置：

- 容器内监听 `0.0.0.0:8000`。
- 主机仅映射 `127.0.0.1:8000`。
- 运行状态保存在 `./runtime/`。
- 首次密钥为 `moyu666`。
- 容器策略为 `unless-stopped`。

自定义端口或首次密钥：

```bash
CHATGPT2API_PORT=8800 CHATGPT2API_INITIAL_AUTH_KEY='change-me-now' sh deploy_server.sh
```

`CHATGPT2API_INITIAL_AUTH_KEY` 只在首次创建 `runtime/config.json` 时使用，不覆盖已有密钥。

更新部署：

```bash
git pull --ff-only
docker compose up -d --build
```

## 反向代理

生产服务器应保留 Compose 的 `127.0.0.1` 绑定，并通过 Nginx、Caddy 或其他网关提供 TLS。网关还应限制来源 IP、增加独立认证或接入私有网络。不要直接把 `8000` 端口暴露到公网。

## 备份与恢复

停止写入任务后备份：

- Windows：`config.json` 与 `data/`
- Docker：整个 `runtime/`

恢复时将备份放回原路径后重启服务。不要把运行备份提交到 Git。
