# 参与贡献

感谢改进 MemWeave。提交代码、文档或问题前，请先确认改动符合当前产品边界：一个 MCP、一个 Skill、低频 Hook、Markdown 权威存储和显式任务队列。

## 开发环境

需要 Python 3.12+。推荐在仓库根目录使用独立虚拟环境：

```powershell
& '<Python 3.12+ 绝对路径>' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.windows-py313.txt
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e .
```

当前锁文件对应 Windows / Python 3.13；其他平台或版本需要重新解析依赖并验证。

## 提交前检查

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m compileall -q src tests scripts
git diff --check
```

定向修改优先运行相关测试；影响安装、客户端配置、存储或 MCP 工具面时，再运行全量回归。文档改动至少检查本地 Markdown 链接。

## 设计约束

- 不增加第二套 MCP、索引、计划文件或上下文构建器。
- 不把逐轮原话、完整对话、命令、工具参数或工具输出写入记录。
- 配置检查和协议探针不能写成桌面客户端已验证。
- 未运行的测试、未观察的 Hook 和未创建的 Release 必须保持未验证。
- 受管配置修改必须保留他人内容、提供冲突提示和可恢复路径。
- WorkUnit 只保护记录并发，不替代独立 worktree 或文件级并发协调。

## 提交与 Pull Request

- 保持改动聚焦，避免顺手重排无关文件。
- 新增行为应有回归测试；纯文档改动应说明链接与事实核对方式。
- PR 描述写清用户可见变化、验证命令、未验证边界和迁移影响。
- 不提交 `.venv/`、本机记忆库、证据日志、真实用户数据、凭据或绝对个人路径。
- 提交前检查 `git status`，不要把生成文件、备份或本地客户端配置带入变更。

Issue 和 PR 中不要粘贴访问令牌、完整 transcript、私有仓库内容或记忆库原文。需要说明问题时使用最小脱敏片段。

## 许可证

贡献即表示同意按仓库的 `AGPL-3.0-or-later` 许可发布；第三方代码或文本必须同时更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 并保留原始许可证。
