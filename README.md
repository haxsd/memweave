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

## 工作模型

MemWeave 只有两层产品语义：

1. **任务工作记忆**：目标、验收、检查点、失败、验证、下一步和并行 WorkUnit。
2. **长期知识**：项目、项目组或用户明确偏好范围内的可复用知识，带来源、状态和修订历史。

文件结构与架构关系是核对记忆是否仍适用于当前代码的支撑证据，不是第三层记忆。

同一项目一次只推进一个主任务，其余主任务按确定顺序排队；当前任务内部仍可并行。项目身份是绝对文件夹，客户端只是适配器。显式登记的子文件夹是独立项目，linked worktree 复用主 checkout 的项目身份。父项目可以只读汇总直接子项目，但不接管子项目的任务；组知识由子项目显式检索，默认不注入。

## 安装：优先让 Agent 完成

MemWeave 推荐由已有 Agent 安装：Agent 可以先检查操作系统、Python、已安装的 MCP 客户端和现有配置，再选择对应路径，避免用户手工找配置文件。

把仓库链接、目标项目绝对路径和正在使用的客户端交给 Agent，并让它从 [AGENT_INSTALL.md](AGENT_INSTALL.md) 开始。可以直接复制：

```text
请阅读 MemWeave 的 AGENT_INSTALL.md，先检查我的操作系统、Python 版本、已安装的 MCP 客户端和现有配置，列出安装计划；确认后再执行。不要覆盖我的自定义 MCP、Hook 或规则。完成后告诉我哪些已验证、哪些还需要我重载客户端。
```

Agent 会先做只读盘点，再运行 `scripts/bootstrap.py --dry-run`；只有确认计划后才写入配置。它负责验证 MCP 工具、Skill/Rule 和启动通道，而不是只把“配置已写入”当成安装完成。

手动安装步骤保留在 [docs/manual-install.md](docs/manual-install.md)，作为 Agent 不可用时的 fallback。

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

## 统一核心与启动契约

**客户端不会改变 MemWeave 的核心能力，但只连接 MCP 不等于完整可用。** 所有 stdio MCP 客户端共享：

- 同一 22 工具 MCP 目录和调用契约。
- 同一任务工作记忆、检查点、失败/验证记录、WorkUnit 和队列语义。
- 同一 `project` / `group` / `user` 长期知识模型和检索规则。
- 同一绝对文件夹项目身份和同一 `workspace_prepare` 恢复入口。
- 同一 Markdown 权威存储和可重建索引。

完整接入需要三层同时成立：

1. **MCP 核心**：工具、任务/知识数据、项目身份和安全写入。
2. **行为层**：Skill 与 always-on Rule 说明何时恢复、检查点、收尾和提炼。
3. **启动通道**：生命周期 Hook、always-on Rule 或等价首轮指令，让模型在第一次项目动作调用 `workspace_prepare`。

Hook 只是启动通道的一种实现，不是唯一实现。没有 Hook 的宿主可以由 always-on Rule 或等价首轮指令启动；没有启动通道时，只能标记“核心已连接，尚未完整接入”。

客户端差异只影响配置位置、资源格式、启动实现和验证证据，不构成核心能力分级。具体边界见 [客户端接入](docs/integrations.md)。

## 在宿主中的工作循环

| 时点 | Agent 应做什么 |
|---|---|
| 开始或恢复 | 先调用 `workspace_prepare`，核对用户目标、当前任务、检查点和实际代码；目标不同就新建任务 |
| 执行 | 在重要发现、失败、阶段变化和验证后保存检查点；并行子代理更新各自 WorkUnit |
| 阶段结束或完成 | 对照验收条件收束阶段；提炼、去重、修订可复用知识，不确定内容保留候选 |
| 下一次相关工作 | 按问题、项目和范围取回知识；尊重纠正、遗忘与来源漂移 |

纯状态查询使用 `inspect` + `task_read`，不取得执行租约。继续工作才使用 `resume`。无关请求用显式 `parent_task_id` 建立侧任务，不进入主队列。

## 架构与边界

- 一个 MCP、一个 Skill 和一个宿主启动通道；宿主支持时由低频 Hook 实现，否则使用 always-on Rule/首轮指令。核心 Agent 工具面为 22 项。
- Markdown 是任务和知识的权威记录；SQLite FTS 是可重建的派生索引。
- 数据默认保存在本机单用户目录（Windows 为 `%USERPROFILE%\.memweave`，其他平台为 `~/.memweave`）；不需要额外模型 API Key。
- WorkUnit 只保护记录并发，不隔离工作树。并行改代码仍需独立 worktree 或互不重叠的文件范围。
- 当前检索为文本 FTS，不使用向量检索；没有匹配时不注入无关知识兜底。
- 记录 ID 不是远程访问凭证；产品当前面向可信的本机单用户。

更完整的设计见 [架构说明](docs/architecture.md) 和 [产品与设计导览](docs/product-guide.html)。

## 项目状态

`0.2.5` 是早期开发预览，尚未创建公开 tag 或 GitHub Release。MCP 核心、22 工具、任务/知识语义和项目身份对所有 stdio MCP 客户端相同；不同宿主的自动启动和压缩恢复能力仍有差异，公开文档只描述已验证的边界。

自动化测试覆盖任务、存储、MCP、安装器、Hook 和客户端巡检；CI 在每次 push 和 pull request 上运行完整 unittest。真实客户端证据、未验证项和版本差异见 [客户端接入](docs/integrations.md)。

## 文档

- [Agent 安装入口](AGENT_INSTALL.md)：把仓库交给已有 Agent 完成环境检查与接入。
- [产品与设计导览](docs/product-guide.html)：单文件静态页，按 8 个章节讲产品逻辑、任务/知识模型和客户端运行方式。
- [架构说明](docs/architecture.md)：两层记忆、运行时、启动契约和边界。
- [手动安装](docs/manual-install.md)：逐项配置、更新与验证。
- [客户端接入](docs/integrations.md)：统一 MCP 核心、各宿主启动通道与验证边界。
- [Agent 使用指引](docs/agent-guide.md)：任务、检查点、知识审查和恢复流程。
- [API 与存储](docs/api.md)：MCP / CLI / HTTP action、记录模型和检索边界。
- [开发说明](docs/development.md)：开发、测试、构建和本地管理页。
- [文档总览](docs/README.md)：全部公开文档。
- [路线图](ROADMAP.md)：高层方向，不承诺具体发布日期。
- [参与贡献](CONTRIBUTING.md)：开发环境、测试门、设计约束和提交要求。
- [安全策略](SECURITY.md)：漏洞报告和本机单用户边界。

## 许可证与来源

MemWeave 以 `AGPL-3.0-or-later` 开源。Basic Memory 是源码级 fork/改编，planning-with-files 是行为与 Skill 级改编；固定上游提交、修改范围和许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
