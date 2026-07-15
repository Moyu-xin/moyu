# 维护与发版

## 日常维护

1. 在独立分支开发，避免直接修改正在运行的数据文件。
2. 运行 Python 测试、编译检查、前端类型检查和生产构建。
3. 在系统设置中确认内存、缓存、Git 状态和重启能力正常。
4. 更新 `CHANGELOG.md` 与 `VERSION`。
5. 使用白名单脚本生成全新发布目录，不从运行目录直接打包。

## 干净发布

```powershell
.\scripts\build_clean_release.ps1 -Destination D:\release\moyu-1.0
```

脚本只复制源代码、测试、部署文件与文档，并拒绝以下内容进入发布包：

- `config.json`、`data/`、`logs/`、`runtime/`
- `.venv/`、`.deps/`、`node_modules/`、构建缓存
- 抓包、审计报告、临时 JSON、私钥和本地备份

在生成目录中执行 `git status`、测试和构建，再连接 GitHub 远端。

## GitHub 首次发布

```bash
git init --initial-branch=main
git add .
git commit -m "release: publish Moyu 1.0"
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

建议启用分支保护、Actions 检查、Dependabot 和 Secret scanning。公开仓库前再次确认不存在运行配置与真实凭据。

## 应用内更新

应用内只支持有 `origin`、有上游分支、工作区干净时的快进更新。它不会执行 `reset --hard`、强制拉取或冲突合并。更新成功后使用侧边栏“重启项目”；启动脚本会检测源码更新时间并自动重新安装依赖和构建前端。Docker 部署应使用 `git pull --ff-only && docker compose up -d --build`。
