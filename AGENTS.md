# MemWeave 开发说明（给 AI 协作者）

MemWeave 是一个本地 stdio MCP 和 Python 包，为已有编程 Agent 提供任务连续性和有来源的长期知识。修改代码前先读 [README](README.md)、[架构说明](docs/architecture.md)、[开发说明](docs/development.md) 和 [参与贡献](CONTRIBUTING.md)。

## 环境

- Python 3.12+；仓库内 `.venv` 可用。
- 安装依赖：`python -m pip install -r requirements-lock.windows-py313.txt`，再 `python -m pip install --no-deps -e .`。
- 不要把 `.venv/`、客户端配置、记忆库、证据日志或凭据提交进仓库。

## 测试

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m compileall -q src tests scripts
git diff --check
```

定向修改先跑相关测试；影响 MCP、安装器、存储或 Hook 时再跑全量。

## 设计边界

- 保持一个 MCP、一个 Skill 和一个启动通道；不要新增第二套运行时。
- 不把逐轮原话、完整对话、工具输出或凭据写入任务/知识记录。
- Markdown 是权威记录，SQLite 只是可重建索引。
- 客户端是适配器，不是能力等级；不要为了某个客户端改变 22 工具核心。
- 安装器修改客户端配置时必须保留已有内容、备份并报告冲突。
- 未运行的测试、未观察的客户端行为不能写成已验证。

## 提交

- 改动聚焦，说明用户可见变化、验证命令、未验证边界和迁移影响。
- 不提交本机路径、真实用户数据、证据日志、凭据或生成文件。
- 许可证为 AGPL-3.0-or-later；第三方代码或文本必须同步更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 并保留原始许可证。
