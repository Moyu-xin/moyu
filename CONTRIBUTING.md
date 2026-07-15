# 贡献指南

提交 Issue 前请删除邮箱、密码、Token、Cookie、代理认证、管理密钥、真实账号 JSON 和完整日志路径。

Pull Request 应满足：

- 变更范围明确，不包含运行数据或无关格式化。
- 新行为附带相应测试。
- Python 与 TypeScript 检查通过。
- 用户可见变更更新文档和 `CHANGELOG.md`。
- 不降低本地绑定、鉴权、路径白名单和 Git 快进更新的安全边界。

提交信息建议使用 `feat:`、`fix:`、`docs:`、`test:`、`chore:` 等清晰前缀。
