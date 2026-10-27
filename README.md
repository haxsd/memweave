# MemWeave · 织忆

> 让任务有连续性，让合作有积累。
>
> Weave every task into lasting understanding.

MemWeave 为已有编程 Agent 提供持续的任务记忆、长期积累和项目理解。任务工作记忆帮助保留目标、发现、失败尝试与验证；长期记忆积累有来源的项目知识和用户明确偏好；文件结构与项目架构两层图谱把这些记忆连接到当前实现。同一 IDE 中的后续工作也应受益，跨会话、跨工具接续是其中的使用场景。

首批客户端为 Codex 桌面版与 Cursor，产品形态沿用 **MCP + Skills + Hooks**，由宿主 Agent 负责理解、提炼与使用。数据保存在本机，服务不独立调用模型，不要求额外配置模型 API 密钥。日常入口位于已有 Agent 中，本地管理页按需辅助查看、纠正与遗忘。

**本轮修订为 0.2.4 工程预览。** 日常 MCP 保持 20 个工具，完整模式保留 27 个；Codex 与 Cursor 都可用 `setup` 接入 MCP、Skill、Hooks，用 `check --probe` 检查配置与协议。Codex Hooks 已通过原生 CLI 的受控事件验证，真实桌面持续体验和核心收益仍待验收。

## 接入已有 Agent

**让 Agent 安装：** 将 [本仓库](https://github.com/haxsd/memweave) 链接与目标项目、客户端交给 Agent，让它读取 [AGENT_INSTALL.md](AGENT_INSTALL.md) 并执行。当前仓库为私有，需要已有 GitHub 访问权。

**手动安装：** 按 [手动步骤](docs/manual-install.md) 准备 Python 3.12+。已有源码和 `.venv` 时，可从源码目录执行：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e .
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root 'D:\my-project' --probe
```

Cursor 换为 `--client cursor`（也是默认客户端）。`setup` 合并项目内 MCP 和 Hooks，放置 Skill（Cursor 另有 Rule），初始化所选记忆库并登记项目，不创建业务任务或扫描。再次运行同步当前安装版本资源，保留其他服务与 Hooks，修改前备份；受管资源有自定义改动时提示合并。首次省略 `--home` 使用 `~/.memweave`，后续沿用项目清单中的记忆库。

`check` 默认只读，`--probe` 验证服务协议，不表示桌面已经加载。自动化可加 `--json`。更新 Python 包后重新运行 `setup` 同步资源；命令不自动下载新版包。Codex 需要宿主信任项目并审查 Hook 定义，支持此功能的 CLI 使用 `/hooks`；Cursor 在现有窗口重载服务。命令不会打开 IDE 窗口。两个客户端应使用同一记忆库。详见 [接入说明](docs/integrations.md)。

在宿主中读取 `memweave_system_status` 后，调用只读 `memweave_workspace_prepare`，提供精确的项目绝对根目录和明确意图：

- `inspect`：查看项目、任务与待提炼概况，不选择已有任务。
- `new`：准备新工作，不恢复旧任务；确认目标与验收条件后再显式创建任务。
- `resume`：恢复显式指定的任务；未指定时只选择唯一活跃任务。多个活跃任务返回 `task_selection_required`，由用户或明确上下文选定，不能按最近时间猜测。

未注册目录返回 `registration_required`，再显式注册并重试。该入口不创建项目、任务或检查点，也不自动扫描。它返回工作区状态、项目与任务简表、待提炼简表及上下文。用户范围知识通过 `include_user` 显式选择；完整参数见 [API](docs/api.md)。

## 在四个时点使用

| 时点 | 宿主 Agent 的工作 |
|---|---|
| 开始或恢复 | 确认工作目录与意图，取回选定任务、已启用知识和有效局部图谱，核对实际代码 |
| 执行 | 在重要发现、失败、阶段变化和验证后保存检查点；按需读取文件证据并维护架构理解 |
| 阶段结束或完成 | 核对验收，提炼、去重和修订可复用知识；不确定内容保留候选，无宿主时保留待办 |
| 下一次相关工作 | 按问题和范围取回经验与结构，尊重纠正、遗忘及来源变化，减少重复探索和说明 |

[Agent 使用指引](docs/agent-guide.md) 说明具体调用顺序。Cursor `setup` 安装包内原创接续 Skill，其他客户端可按实际支持的目录复制。资源安装不代表模型行为收益。Cursor Hooks 返回启动上下文与有界跟进，实际理解、检查点与提炼仍由宿主通过 MCP 完成。

## 可用能力

| 能力 | 工程行为 |
|---|---|
| 任务接续 | 独立任务 ID、目标与验收条件、检查点历史、最新下一步、完成与取消 |
| 长期知识 | project / task / user 范围、明确来源、候选与显式激活、中文和英文词法检索、修订、遗忘 |
| 知识提炼 | 完成任务后保留待提炼状态，宿主获取证据、去重并记录提炼结果 |
| 项目图谱 | 有界文件扫描、嵌套忽略规则、文件哈希、架构证据、来源变化后待核对 |
| 工作区准备与上下文 | 只读识别精确根目录与任务意图，按总字符预算组合任务、已启用知识和有效局部图谱 |
| 写入保护 | 操作幂等、版本与内容指纹校验、跨进程写锁、中断恢复、冲突保留外部内容 |
| 使用入口 | Agent 内具名 MCP 工具与可复制 Skill，辅以 JSON CLI 和可选本地管理页 |
| 接入与维护 | Codex / Cursor 项目 setup / check；默认 20 个工具、full 27 个；Codex 七类、Cursor 六类 Hooks；Agent 引导与手动安装 |

检索是本地词法匹配，尚无向量检索。图谱查询只核对已索引来源，新文件需要重新扫描。工作区根目录分别注册，尚不自动合并克隆或 worktree。知识遗忘删除当前文档和逻辑索引，不承诺清除外部备份、已发出的上下文或磁盘残留。此版本面向可信本机单用户，记录 ID 不是独立的访问凭证。

上下文返回实际纳入的 `included_sources` 和截断状态，并提供图谱覆盖与新鲜度摘要。来源漂移的架构默认不进入有效上下文；新增文件仍需显式重扫。

Cursor 试用工作区此前已显示 MCP 连接与 27 个工具；更新后新启动的服务默认返回 20 个。真实桌面工具调用、自动 Hook 触发及四时点流程仍待客户端验收；SDK stdio 与 Hook 子进程验证不能代替桌面验收。尚未完成公平模型对照，不能据此认定遗漏、重复探索或用户纠正成本已降低。

## 可选辅助与诊断

需要查看、纠正或遗忘记录时，可双击本目录 `start.cmd`，或执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave ui --home '.\work\runtime\memory home'
```

管理页只监听 `127.0.0.1`，每次启动生成访问令牌；关闭时在终端按 `Ctrl+C`。端口占用时加 `--port 0`，不打开浏览器时加 `--no-browser`。日常 Agent 工作无需打开它。

`memweave doctor` 检查本地状态，`memweave probe` 验证 SDK stdio 与重启读取；它们不代替正式任务记忆或桌面验收。

## 运行完整演示

```powershell
& '.\.venv\Scripts\python.exe' -m memweave demo --home '.\work\runtime\memory home' --root .
```

演示实际启动两个独立 MCP 服务进程，执行检查点恢复、知识检索与纠正、临时知识遗忘、扫描与架构证据记录，并保留带 `【演示】` 标记的数据。它验证工程链路，不代表真实宿主已持续记录、主动提炼或复用经验。

安装、测试和构建见 [开发说明](docs/development.md)，客户端配置见 [接入说明](docs/integrations.md)，工具参数与恢复流程见 [API 与存储说明](docs/api.md)。运行证据与研究原始资料留在本机，不随源码上传。

## 文档入口

- [Agent 安装入口](AGENT_INSTALL.md)：由已有 Agent 准备环境和接入资源。
- [手动安装](docs/manual-install.md)：逐项配置与更新。
- [Agent 使用指引](docs/agent-guide.md)：任务与知识维护循环。
- [2026-10-27 目标修正基准](docs/specs/2026-10-27-product-realignment.md)：本轮四时点行为、只读入口与验收边界。
- 本机开发区的 `context/CURRENT_STATE.md` 保存接续状态；`outputs/` 和 `docs/history/` 保存前期方案与讨论，均不随源码分发。

## 已确认方向

| 项目 | 选择 |
|---|---|
| 核心语言 | Python |
| 首批客户端 | Codex 桌面版、Cursor |
| 产品形态 | MCP + Skills + Hooks，辅以本地 CLI |
| 存储 | Markdown + SQLite |
| 图谱 | 文件结构、项目架构两层；基础能力不限定项目语言 |
| 理解与提炼 | 由宿主 Agent 完成，首版不独立调用模型 |
| 核心收益 | 工作记忆提升任务可靠性，长期积累改善合作体验 |

## 开发区

`outputs/` 保留迁入文档的原名和原内容；`work/` 保留为工作产物目录。历史讨论中的较早建议可能已被后续决策取代，当前方向以状态文档和两份方案中的最新明确选择为准。
