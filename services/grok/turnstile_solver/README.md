# turnstile-solver

`grokcli-2api` 子模块：本地 Cloudflare Turnstile 过盾。

默认以**内联方式**运行在主容器 `grokcli-2api` 内（同一容器，loopback `127.0.0.1:5072`）。

## 运行方式（推荐）

主容器启动时，`entrypoint.sh` 自动拉起：

```bash
cd /root/grokcli-2api
docker compose up -d --build grokcli-2api
```

容器内地址：

```text
http://127.0.0.1:5072
```

环境变量：

```env
GROK2API_CAPTCHA_PROVIDER=local
GROK2API_LOCAL_SOLVER_URL=http://127.0.0.1:5072
GROK2API_INLINE_SOLVER=1
TURNSTILE_THREAD=3
```

## 协议

- `POST /createTask`
- `POST /getTaskResult`

## 日志

```bash
docker exec grokcli-2api tail -n 100 /app/turnstile-solver/logs/turnstile_solver.log
# 或宿主机映射
tail -n 100 /root/grokcli-2api/turnstile-solver/logs/turnstile_solver.log
```

## 可选：宿主机单独启动

仅调试用：

```bash
./start.sh
./stop.sh
```
