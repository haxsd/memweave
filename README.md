# MemWeave · 织忆

> 让任务有连续性，让合作有积累。
>
> Local task continuity and reviewed project knowledge for MCP coding agents.

MemWeave 是已有编程 Agent 的本地连续工作层。它把任务目标、约束、失败尝试、验证结果和下一步从聊天上下文中独立出来，并把真正可复用的经验沉淀为有范围、有来源的长期知识。

它不替代 Codex、Cursor、Devin 或其他宿主，也不启动自己的模型。宿主 Agent 负责理解、判断和写代码；MemWeave 通过一个常驻 MCP 提供可恢复的工作记忆。

## 解决什么问题

- **长任务不跑偏**：恢复目标、验收、最新进展、已否定方案和唯一下一步，而不是重新读一遍聊天记录。
- **新会话接着做**：任务状态保存在本机 Markdown，不依赖某个聊天线程。
- **换客户端可接续**：同一项目文件夹由不同 Agent 打开时，复用同一项目、任务队列和知识。
- **完成后不重复踩坑**：任务收束时从证据提炼知识，经过显式审查后才进入后续召回。
- **并行但不丢状态**：父任务下可以维护多个 WorkUnit，子代理共享父任务，各自更新独立记录。

## 安装：优先让 Agent 完成

MemWeave 推荐由已有 Agent 安装：Agent 可以先检查操作系统、Python、已安装的 MCP 客户端和现有配置，再选择对应路径，避免用户手工找配置文件。

把仓库链接、目标项目绝对路径和正在使用的客户端交给 Agent，并让它从 [AGENT_INSTALL.md](AGENT_INSTALL.md) 开始。可以直接复制：

```text
请阅读 MemWeave 的 AGENT_INSTALL.md，先检查我的操作系统、Python 版本、已安装的 MCP 客户端和现有配置，列出安装计划；确认后再执行。不要覆盖我的自定义 MCP、Hook 或规则。完成后告诉我哪些已验证、哪些还需要我重载客户端。
```

Agent 会先做只读盘点，再运行 `scripts/bootstrap.py --dry-run`；只有确认计划后才写入配置。它负责验证 MCP 工具、Skill/Rule 和启动通道，而不是只把“配置已写入”当成安装完成。

手动步骤保留在本文的通用路径和 bootstrap 命令中，作为 Agent 不可用时的 fallback。

## 快速开始（给已经装好 Python 的人）

要求 Python 3.12+，从完整源码克隆执行：

```powershell
& '<Python 3.12+ 绝对路径>' scripts/bootstrap.py --client codex --root 'D:\my-project' --json
```

`--client` 支持 `codex`、`cursor` 和 `devin`。脚本复用或创建仓库内 `.venv`，安装当前源码，执行 `setup` 和 `check --probe`。先加 `--dry-run --json` 可以只看计划。

其他支持 stdio MCP 的客户端使用通用配置：

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave init --home "$HOME\.memweave"
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave config --client generic --home "$HOME\.memweave"
```

把输出中的 `mcpServers.memweave` 合入客户端配置，并把 [接续 Skill](src/memweave/skills/memweave-continuity/SKILL.md) 复制到客户端支持的技能目录。还必须安装一条 always-on Rule 或等价首轮指令，要求首个项目动作调用 `workspace_prepare`；只连接 MCP 不算完整接入。

## 它如何工作

MemWeave 只有两层产品语义：任务工作记忆（目标、验收、检查点、失败、验证、下一步和并行 WorkUnit），以及长期知识（按项目、项目组或用户明确偏好划分范围，带来源、状态和修订历史）。数据以本机 Markdown 为权威记录，默认保存在 `~/.memweave`（Windows 为 `%USERPROFILE%\.memweave`），不需要额外的模型 API Key。只连接 MCP 不等于完整可用。

文件夹身份、worktree、组知识、三层接入契约、会话工作循环和架构边界，见 [产品与设计导览](docs/product-guide.html)。

## 项目状态

`0.2.5` 已经公开，克隆本仓库并按上面的安装步骤即可使用。当前仍是早期版本，能力和文档还会继续完善。CI 在每次 push 和 pull request 上运行完整 unittest。

## 文档

- [Agent 安装入口](AGENT_INSTALL.md)：把仓库交给已有 Agent 完成环境检查与接入。
- [产品与设计导览](docs/product-guide.html)：单文件静态页，按 8 个章节讲产品逻辑、任务/知识模型和客户端运行方式。

## 许可证与来源

MemWeave 以 `AGPL-3.0-or-later` 开源。Basic Memory 是源码级 fork/改编，planning-with-files 是行为与 Skill 级改编；固定上游提交、修改范围和许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
