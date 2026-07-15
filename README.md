## 免责声明

本项目仅供学习、研究和技术交流使用，不提供任何账号、支付、代理、短信、邮箱或其他第三方服务资源。

使用者应自行确认其使用方式符合目标网站或平台的服务条款、当地法律法规和相关合规要求。严禁将本项目用于违法违规用途、商业滥用、绕过平台限制、批量注册、攻击、爬取、骚扰、欺诈或任何侵犯他人权益的行为。

项目作者和维护者不对因使用、修改、部署或分发本项目而产生的任何直接或间接后果负责。使用本项目即表示你理解并自行承担全部风险。

如本项目中的内容涉及第三方权益，请通过仓库 Issue 联系处理。

交流群：1062966376
# 摸鱼机 Moyu 1.0

摸鱼机是一套本地优先的注册任务与账号库存管理面板，集中管理邮箱、代理、注册任务、成品/半成品账号、OAuth、K12、Plus 提链和中转仓库。系统设置提供实时资源监控、内存预测、登录密钥热更新、日志缓存清理、Git 快进更新与本地项目重启。

> 默认仅监听 `127.0.0.1:8000`。首次登录密钥为 `moyu666`，登录后请在“系统设置”中更换。

## 主要功能

- 注册任务：Free/K12、成品/半成品目标、并发控制、实时进度与日志。
- 邮箱库存：Outlook 库存与分组，以及 Gmail、Cloudflare、DuckMail 等提供商配置。
- 代理配置：订阅、节点池、Mihomo、连通性测试和账号节点粘性。
- 配置热更新：保存邮箱或代理配置不会中断注册；进行中的任务保留启动时快照，后续任务使用新配置。会改变底层代理进程的启动、停止和手动切换操作在注册运行期间会被阻止。
- 账号库存：成品与半成品采用一致的筛选、编辑、测活、额度、OAuth、导入导出和仓库同步能力。
- 大库存优化：邮箱、成品和半成品库存先加载首屏并在后台补齐完整数据，栏目悬停预取且已访问页面保持驻留。
- Plus/K12/仓库：批量任务、状态追踪和失败原因记录。
- 系统设置：密钥热更新、内存监控与预测、日志缓存清理、一键重置、安全 Git 更新。
- 部署维护：Windows 单命令首次启动、Docker Compose 服务器部署、干净发布包生成和 CI。

## 快速开始

### Windows 本地

环境要求：Windows 10/11、PowerShell 5.1+、Python 3.13、Node.js 20.9+ 与 Corepack。

```powershell
.\start_local.ps1
```

首次运行会自动执行初始化、创建 `.venv`、生成 `config.json`、安装依赖并构建前端。访问：

<http://127.0.0.1:8000/register/>

停止服务：

```powershell
.\stop_local.ps1
```

### Linux / 服务器 Docker

环境要求：Docker Engine 与 Docker Compose 插件。

```bash
sh deploy_server.sh
```

运行数据保存在 `runtime/`，更新或重建容器不会覆盖密钥与库存。默认只映射到服务器本机 `127.0.0.1`；远程访问应通过带 TLS 和访问控制的反向代理发布。

详细步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)，配置字段见 [CONFIGURATION.md](CONFIGURATION.md)。

## 首次配置顺序

1. 使用 `moyu666` 登录，立即在“系统设置”更换密钥。
2. 在“邮箱”中配置邮箱来源、凭据和库存分组。
3. 在“代理配置”中添加订阅或节点并完成连通性检测。
4. 在“注册”中选择注册模式、产物类型、邮箱分组与并发数。
5. 在成品或半成品库存中继续测活、OAuth、导出或中转仓库同步。

账号网络操作会优先复用注册时记录的节点，减少注册、测活和 OAuth 之间的出口变化，但不对账号状态或存活率作任何保证。

## 数据与安全

- 运行数据：`data/`，Docker 部署为 `runtime/data/`。
- 登录配置：`config.json`，Docker 部署为 `runtime/config.json`。
- 日志：`logs/`，Docker 部署为 `runtime/logs/`。
- 上述目录均被 Git 与干净发布脚本排除。
- Git 更新仅执行 `fetch` 和 `merge --ff-only`；工作区有未提交修改时会拒绝更新。
- 一键重置只恢复系统监控参数，保留密钥、账号、邮箱与代理库存。

安全边界和公开部署要求见 [SECURITY.md](SECURITY.md)，使用边界见 [DISCLAIMER.md](DISCLAIMER.md)。

## 开发与验证

```powershell
python -m pip install -e ".[test]"
python -m unittest discover -s tests
python -m compileall -q api services scripts utils main.py serve_local.py
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm typecheck
corepack pnpm build
```

生成全新的 GitHub 干净副本：

```powershell
.\scripts\build_clean_release.ps1 -Destination D:\path\moyu-github-clean
```

维护与发版流程见 [MAINTENANCE.md](MAINTENANCE.md)，贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 参考项目

- [basketikun/chatgpt2api](https://github.com/basketikun/chatgpt2api)
- [FastAPI](https://github.com/fastapi/fastapi)
- [Next.js](https://github.com/vercel/next.js)
- [curl_cffi](https://github.com/lexiforest/curl_cffi)
- [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo)
- https://github.com/ZeroPointSix/outlookEmailPlus
- https://github.com/Wei-Shaw/sub2api
- https://github.com/router-for-me/CLIProxyAPI
- https://github.com/wenfxl/openai-cpa
- 以及部分非Github公开项目,感谢以上开发者的开源付出

本项目为独立社区项目，与 OpenAI 无隶属、授权或背书关系。

## 赞助

暂无，联系方式qq:1553622195

## 许可证

项目按 [LICENSE](LICENSE) 发布。使用前请同时阅读 [DISCLAIMER.md](DISCLAIMER.md)。
