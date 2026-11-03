# MemWeave · 织忆

> 让任务有连续性，让合作有积累。

> 2026-10-29 验证更正：Codex 桌面新会话自动恢复通过；Cursor 原生 MCP 工具可用，但 Cursor 3.27.22 的 `sessionStart` 尽管返回了 `additional_context`，模型仍没有收到启动块。该事件的官方契约是 fire-and-forget，因此只能按 best effort 处理；当前真实恢复路径是常驻 MCP + 项目 Rule/Skill 首轮 `workspace_prepare`。普通首轮和检查点后新会话都已实测。Cursor MCP 按项目/仓库启用一次。
>
> Weave every task into lasting understanding.

**2026-10-27 方向重新确认：AGPL-3.0-or-later 完全开源，深度融合 Basic Memory 与 planning-with-files。** MemWeave 将直接以 Basic Memory 的 Markdown + SQLite、检索和上下文能力为底座，移植 planning-with-files 的任务生命周期与 Hook 机制，最终形成一个 MCP、一个 Skill、一个 Hook 入口。不是并排运行两个成熟项目，也不再重写一套更弱的存储、检索或图谱引擎。完整目标与阶段见 [PLAN.md](PLAN.md)。

产品语义收敛为两层：**任务工作记忆**保存目标、约束、检查点、失败尝试、验证和下一步，支持上下文压缩、同项目新会话和跨客户端接续；**长期知识**保存任务或阶段后真正可复用的项目知识与用户明确偏好。文件结构与架构关系是连接记忆和当前代码的支撑证据，不是第三层产品。同一项目一次只推进一个顶层任务，其余按确定顺序排队；当前任务内部可用多个持久 WorkUnit 并行推进子代理工作。

首批客户端为 Codex 桌面版与 Cursor，由宿主 Agent 负责理解、提炼与使用。数据保存在本机，服务不独立调用模型，不要求额外配置模型 API 密钥。日常入口位于已有 Agent 中；本地管理页按需辅助查看项目状态、知识与架构记录，并执行纠正或遗忘。

### 记忆边界与存放（当前语义）

长期知识不是一套“所有项目共享的全局事实”。本机只有一个 Markdown 记忆库 `%USERPROFILE%\.memweave`，每条知识带适用范围，检索时按当前项目/任务过滤：

| scope | 可以使用 | 不会自动进入 |
|---|---|---|
| `project` | 同一 `project_id` 的后续任务和新会话 | 其他项目 |
| `task` | 同一 `task_id` 的任务 | 其他任务、其他项目 |
| `user` | 用户明确要求长期记住的偏好或规则；来源必须包含 `user`，不能绑定 task；必须显式 `include_user` | 未显式选择的普通上下文、项目事实 |

任务、检查点、WorkUnit 和 phase 是工作记忆，分别保存在 `tasks/`、`checkpoints/`、`work_units/`、`phases/`，不会自动变成长期知识。检查点与阶段使用独立 Markdown，任务正文只保留目标、验收和最新摘要；完成并审查的任务进入可查询归档，不自动删除。只有经宿主审核后写入 `knowledge/` 的 Markdown 才是可复用知识；`memory_review` 只准备证据，`memory_review_complete` 只记录审查结果，不会自动提炼或激活。MCP 的 `memweave_memory_write` 默认生成 `candidate`，显式激活后才进入默认检索；CLI/管理页中的用户明确录入可以显式选择 `active`。`index/basic-memory/` 是从 Markdown 重建的索引，不是第二份记忆。

**当前开发版已完成 Phase 0–16，实现长期项目有界历史、低频 Hook、单一通用工具面，并在 Codex/Cursor 真实会话验证连续工作恢复。** 用户项目不再用累计 Phase 表示批次；每个有明确目标与验收的需求批次仍是一个有界任务。完成并知识审查后进入可查询归档，默认 `workspace_prepare` 只装配 current、queued、待审查项、计数和最多 5 条近期归档；`task_list` 提供分页、状态与摘要查询。检查点与阶段写入独立 Markdown，任务正文不再随每次操作无限增长。Codex 的默认 Hook 面为 5 类（`SessionStart`、`PreCompact`、`UserPromptSubmit`、`Interrupt`、`SessionEnd`），Cursor 为 2 类；`PreCompact` 只保存压缩前有界快照，`UserPromptSubmit` 只在待恢复快照存在时注入，普通输入快速返回。文件编辑、工具调用、逐轮原话检索和每轮 Stop 维护继续走常驻 MCP，不提供高频/轻量的双轨选择。Phase 13 将默认工具面收敛为单一通用的 22 个 MCP 工具，旧 `--toolset` 只保留迁移兼容；诊断、恢复和管理动作走 CLI/UI。Basic Memory `0.24.1` 仍是 Markdown/SQLite 单底座，活动库位于 `%USERPROFILE%\.memweave`。Codex CLI 与桌面应用已产生低频 `SessionStart`/`SessionEnd` 收据；Codex 新会话及真实压缩后恢复均通过。Cursor 原生 Agent 在未提供任务 ID 的普通提示下，通过常驻 MCP 自动恢复同一任务；检查点后新会话也能恢复到新 revision。Cursor 的 `sessionStart` 仅按 best effort 记录，`hook_sessions.startup_context_emitted=true` 不代表模型已收到。Cursor 结束事件和统计收益仍需观察；0.2.5 私有开发快照已提交并推送，公开标签和 GitHub Release 尚未执行。目标行为见 [AGPL 深度融合目标规格](docs/specs/2026-10-27-agpl-fusion-target.md)，融合前的产品行为作为历史基准保留在 [产品行为基准](docs/specs/2026-10-27-product-realignment.md)。

**Phase 15 已完成（2026-11-02）。** 3 个真实首轮样本已验证：Codex 最终状态 26.0 秒；Cursor 修复前 230.4 秒，收紧首轮路径后 75.3 秒，后续首个项目动作是 MCP `workspace_prepare`。Cursor 的 Hook 仍按 best effort 记录，不能把收据当作模型已收到启动块。干净 wheel、独立 venv、217 项全量回归、`clients doctor` 8/8、敏感信息和 Git 状态发布门均已在本地核对；提交与推送已获用户授权，标签和 Release 仍未执行。

**Phase 16 已完成（2026-11-02）。** 根目录 wheel 增量构建污染已修复：`setup.py` 在每次构建前清理生成的 `build/lib`，坏 wheel 从 528 个条目降到 299 个，157 个已删除 Basic Memory 入口全部消失。新增打包回归测试；干净 venv 的 `pip check`、`init`、`doctor`、stdio `probe`，活动 home 的全新 MCP 进程恢复、`clients doctor` 8/8、Codex/Cursor 各 22 工具探针和 218 项全量回归均通过。后续 P1 修复了 Codex 恢复线程时 MCP 工具目录晚于回合启动的竞态：重型 Service 改为首次调用初始化，工具目录提前到约 1.34 秒，并明确禁止在工具未出现时凭文案调用；最终全量 219 tests / 1 skipped / OK（656.644 秒）。这些改动随本次稳定化提交进入 `main`。

**2026-11-02 Codex 压缩恢复 P1。** 此前真实线程的三次 `contextCompaction` 没有产生压缩 Hook 收据。本版增加 `PreCompact` 有界快照和按需 `UserPromptSubmit` 恢复：快照包含当前任务、最近用户/助手文本和工具名称，不保存命令、工具参数、工具输出、transcript 路径或完整对话；捕获与消费不启动搜索索引。无 pending 时快速返回且不写逐轮 receipt。恢复上下文包含最新 findings，默认近期归档只返回标识摘要。首次真实压缩恢复块已在本会话中可见；本轮修订仍需后续真实样本，同一回合自动续跑的交付尚不能保证。

## 接入已有 Agent（当前开发版）

**让 Agent 安装：** 将 [本仓库](https://github.com/haxsd/memweave) 链接与目标项目、客户端交给 Agent，让它读取 [AGENT_INSTALL.md](AGENT_INSTALL.md) 并执行。AGPL 许可证与第三方声明已在 Phase 0 落地；可公开发布准备的代码与 wheel 已完成验证。0.2.5 开发快照已提交并推送到私有仓库，尚未建公开标签或 GitHub Release。

**手动安装：** 按 [手动步骤](docs/manual-install.md) 准备 Python 3.12+。已有源码和 `.venv` 时，可从源码目录执行：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e .
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root 'D:\my-project' --probe
& '.\.venv\Scripts\python.exe' -m memweave clients doctor
```

Cursor 换为 `--client cursor`（也是默认客户端）。`setup` 合并项目内 MCP 和 Hooks，放置 Skill（Cursor 另有 Rule），初始化所选记忆库并登记项目，不创建业务任务或扫描。再次运行同步当前安装版本资源，保留其他服务与 Hooks，修改前备份；受管资源有自定义改动时提示合并。首次省略 `--home` 使用 `~/.memweave`，后续沿用项目清单中的记忆库。

`check` 默认只读，`--probe` 验证服务协议，不表示桌面已经加载。自动化可加 `--json`。更新 Python 包后重新运行 `setup` 同步资源；命令不自动下载新版包。Codex 需要宿主信任项目并审查 Hook 定义，支持此功能的 CLI 使用 `/hooks`；Cursor 在现有窗口重载服务。命令不会打开 IDE 窗口。两个客户端应使用同一记忆库。详见 [接入说明](docs/integrations.md)。

`clients doctor` 扫描活动库已登记项目和 Codex 已知项目，也可用多个 `--root` 追加目录。它报告旧 home、解释器/服务失效、MCP/Hook 缺失、受管资源漂移和全局/项目入口冲突；有项目级可修复项时加 `--fix` 复用 `setup` 并保留备份。命令不会擅自改写全局客户端配置，修复后仍需重载客户端，配置检查也不等于桌面验收。

在宿主中读取 `memweave_system_status` 后，调用只读 `memweave_workspace_prepare`，提供精确的项目绝对根目录和明确意图：

已安装 Hook 时，先读取会话启动时注入的 MemWeave 块。唯一的 `current` 任务是**恢复候选**，而不是强制目标；若当前用户明显开始新工作，改用 `new`，不得沿用旧进度。`queued` 任务不会被自动抢占。普通召回、任务绑定和检查点维护由常驻 MCP 完成。Codex 的 `PreCompact` 保存压缩前快照，`UserPromptSubmit` 只在下一次用户输入且快照待恢复时注入；普通输入只做轻量状态检查，不检索、不写 receipt。同一回合压缩后自动续跑的注入仍未验证。Cursor 仍只使用 `sessionStart/sessionEnd`。

- `inspect`：查看项目、任务与待提炼概况，不选择已有任务。
- `new`：准备新工作，不恢复旧任务；确认目标与验收条件后再显式创建任务。
- `resume`：恢复唯一 `current` 任务；显式提供的任务也必须是 current。新任务在有 current 时进入队列，关闭 current 后按 `queue_position` 自动推进下一项。

未注册目录返回 `registration_required`，再显式注册并重试。该入口不创建项目、任务或检查点，也不自动扫描。它返回工作区状态、项目与任务简表、待提炼简表及上下文。用户范围知识通过 `include_user` 显式选择；完整参数见 [API](docs/api.md)。

## 在四个时点使用（当前开发版）

| 时点 | 宿主 Agent 的工作 |
|---|---|
| 开始或恢复 | 确认工作目录与意图，取回 current 任务、运行中/阻塞 WorkUnit、已启用知识和有效局部图谱，核对实际代码 |
| 执行 | 在重要发现、失败、阶段变化和验证后保存检查点；并行子代理各自更新 WorkUnit；按需读取文件证据并维护架构理解 |
| 阶段结束或完成 | 核对验收，提炼、去重和修订可复用知识；不确定内容保留候选，无宿主时保留待办 |
| 下一次相关工作 | 按问题和范围取回经验与结构，尊重纠正、遗忘及来源变化，减少重复探索和说明 |

[Agent 使用指引](docs/agent-guide.md) 说明具体调用顺序。`setup` 安装包内原创接续 Skill，其他客户端可按实际支持的目录复制。Codex 与 Cursor 的启动 Hook 尝试恢复唯一进行中的任务；Cursor `sessionStart` 是 fire-and-forget，启动块未出现并不意味着 MCP 不可用，Agent 必须先用常驻 MCP 的 `workspace_prepare` 恢复。普通召回由 Agent 在常驻 MCP 中完成；Codex 的 `UserPromptSubmit` 只检查压缩恢复快照，不按每轮做原话检索。实际理解、检查点与提炼仍由宿主通过 MCP 完成，资源安装不代表模型行为收益。

## 当前开发版能力

| 能力 | 工程行为 |
|---|---|
| 任务接续 | 同项目单 current、确定 queued 顺序；检查点保存为独立 Markdown，任务字段保留最新发现、进展、失败、验证和下一步；关闭 current 自动推进下一项 |
| 接续摘要与门禁 | `workspace_prepare`/`context_get` 返回五问 reboot、attention 与 completion readiness；readiness 不是验收通过，`task_close` 仍要求逐项 host-reported 结果 |
| 任务契约漂移 | 新任务记录 goal+acceptance digest；外部改动显示 `task_contract_drift`，只有 `task_checkpoint(attest_contract=true)` 才能重新确认 |
| 并行执行 | current 任务下可有多个持久 WorkUnit，各自记录负责人、状态、阻塞、进展与下一步；父任务汇总验收 |
| 阶段收束 | 阶段可独立结束并触发知识审查，不要求关闭整个任务 |
| 长期知识 | project / task / user 适用范围、明确来源、候选与显式激活、Basic Memory SQLite FTS5 检索、双语检索提示、修订、遗忘；project/task 不跨项目，user 需显式选择 |
| 知识提炼 | 完成任务后保留待提炼状态，宿主获取证据、去重并记录提炼结果 |
| 项目图谱 | 有界文件扫描、嵌套忽略规则、文件哈希、架构证据、来源变化后待核对 |
| 架构证据生命周期 | `graph_query` 返回 revision/etag/stale_reasons；`graph_update`/`graph_forget` 重新校验来源，使用原子提交与 tombstone |
| 管理页架构维护 | 当前项目 scope/status 与架构 stale 同源显示；架构节点可查看来源、关系、revision、etag 和过期原因；显式重扫后纠正，确认词保护的遗忘走 tombstone |
| 工作区准备与上下文 | 只读识别绝对执行根与任务意图；同一 Git 仓库的主 checkout 与 linked worktree 共用一个项目和任务队列，子目录与不同 clone 不自动归并；按总字符预算组合 current 任务、WorkUnit、已启用知识和有效局部图谱 |
| 长期历史 | 完成并审查的任务进入可查询归档；`workspace_prepare` 只返回当前队列、待审查项、计数和最多 5 条近期归档；`task_list` 支持分页、状态和摘要查询 |
| 写入保护 | 操作幂等、版本与内容指纹校验、跨进程写锁、中断恢复、冲突保留外部内容 |
| 使用入口 | Agent 内具名 MCP 工具与可复制 Skill，辅以 JSON CLI 和可选本地管理页 |
| 接入与维护 | Codex / Cursor 项目 setup / check、跨项目 clients doctor / fix，并巡检已登记仓库的 linked worktree；默认 22 个通用工具，不提供 `core/full` 选择；Codex 注册 5 个生命周期事件（含压缩快照与按需恢复），Cursor 注册 2 个；旧的逐工具、逐编辑和逐轮 Stop Hook 由 setup 清理 |

MemWeave 不再通过逐轮 Hook 驱动 Agent 续跑。默认 Rule、Skill 和 MCP 指令要求同一用户请求内的普通步骤连续完成，只有请求完成、真实阻塞、必须决策或破坏性操作才返回；检查点和知识维护静默执行。客户端何时结束一轮仍由宿主控制，MemWeave 不应伪造无限会话，也不会用高频 Hook 把“中间汇报”变成额外停机点。

并行 WorkUnit 的独立 revision/etag 只保护任务记录，不隔离多个 Agent 的工作树。并行改代码时应使用各自 worktree，或严格划分互不重叠的文件范围；父 Agent 负责合并、验证和任务级检查点。

知识检索由 Basic Memory 的 SQLite FTS5 提供，不再维护 MemWeave 自己的词法索引；目前没有语义/向量检索。上下文由 MemWeave 的预算化 builder 组织，但知识候选来自 Basic Memory 的单一 FTS 检索，并附带其解析出的关系/observation 覆盖；当前任务标题与目标也会作为一次有限查询。没有匹配就不注入近期知识兜底，宿主必须对照当前目标与代码，不能据此宣称语义理解。长期知识可用 `Retrieval cues:` 记录中英召回词。图谱查询只核对已索引来源，新文件需要重新扫描。同一 Git common directory 的主 checkout 与 linked worktree 共用一个项目和任务队列，worktree 只作为执行根；不同 clone 仍分别登记。worktree 需要能让当前目录加载 MCP、Skill 和 Hook 的受管客户端安装；缺失时 `clients doctor` 报告 `worktree_unconfigured`，`--fix` 只修复该执行根的项目级配置并保留备份。父项目 Hook 检测到独立的嵌套 MemWeave 项目时保持沉默，避免双注入。知识遗忘删除当前文档和派生索引，不承诺清除外部备份、已发出的上下文或磁盘残留。此版本面向可信本机单用户，记录 ID 不是独立的访问凭证。

上下文返回实际纳入的 `included_sources` 和截断状态，并提供图谱覆盖与新鲜度摘要。来源漂移的架构默认不进入有效上下文；新增文件仍需显式重扫。

本机原生 Codex CLI 已用受控 fixture 和真实模型验证启动上下文、任务恢复、知识引用与提示词不落库。一次 2026-10-28 的对照中，完整记忆取回了可见测试未编码的范围策略，但耗时和 token 高于详细需求基线，样本仅一轮，因此只能证明知识召回，不能宣称统计收益。Cursor `3.27.14` 桌面已完成一次跨客户端接续；Cursor `3.27.22` 的 `D:\project` 和 `github-radar` 通过常驻 MCP、项目规则与可见状态行完成过真实任务创建/关闭和知识审查，但当前构建的 `sessionStart` 启动块没有进入模型上下文，不能把当时的 Hook 输出或收据写成“模型已收到注入”。历史上 `Stop`、`afterFileEdit`、`afterMCPExecution`、`UserPromptSubmit` 等逐轮或逐工具 Hook 已有触发样本；Phase 12 已移除其它高频事件，2026-11-02 只对 Codex 加入 `PreCompact` 压缩快照和按需 `UserPromptSubmit`，旧证据只作为历史。Codex CLI 与桌面应用已产生成功 `SessionEnd` 样本；Cursor 原生 Agent 已产生 `sessionStart` 输出，并通过 MCP 首轮恢复验证连续性；Cursor 结束事件与统计收益仍未完整验证；Cursor 仍不安装 `preCompact`，客户端状态字段仍不得由配置自检改成通过。

## 可选辅助与诊断

需要查看、纠正或遗忘记录时，可双击本目录 `start.cmd`，或执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave ui --home "$HOME\.memweave"
```

要从当前仓库直接打开工作台，可加 `--root .`；目录启动脚本 `start.cmd` 已自动传入本目录。管理页优先选中该根目录，其次恢复上次选择，避免默认落到其他项目。

管理页只监听 `127.0.0.1`，每次启动生成访问令牌；关闭时在终端按 `Ctrl+C`。端口占用时加 `--port 0`，不打开浏览器时加 `--no-browser`。日常 Agent 工作无需打开它。

工作总览以当前任务、目标和下一步为首屏，并显示任务队列摘要、最近检查点、并行 WorkUnit 与当前项目知识预览。跨项目的“全局记忆状态”收在项目知识面板底部，不与当前项目统计并排重复；当前项目预览不混入跨项目用户知识。项目图谱会优先显示架构节点，并包含默认查询隐藏的 stale 记录。纠正时必须使用页面上显示的 revision/etag；若来源已变化，必须显式勾选重新扫描并同步当前哈希，不能静默覆盖。遗忘要求输入“遗忘”，并说明 Markdown、索引与关系入口的删除及 tombstone 行为。

`memweave doctor` 检查本地状态，`memweave clients doctor` 检查所有已知客户端入口，`memweave probe` 验证 SDK stdio 与重启读取；它们不代替正式任务记忆或桌面验收。

## 运行完整演示

```powershell
& '.\.venv\Scripts\python.exe' -m memweave demo --home "$HOME\.memweave" --root .
```

演示实际启动两个独立 MCP 服务进程，执行两个并行 WorkUnit 的恢复与完成、阶段收束、检查点恢复、知识检索与纠正、临时知识遗忘、扫描与架构证据记录，并保留带 `【演示】` 标记的数据。它验证工程链路，不代表真实宿主已持续记录、主动提炼或复用经验。

安装、测试和构建见 [开发说明](docs/development.md)，客户端配置见 [接入说明](docs/integrations.md)，工具参数与恢复流程见 [API 与存储说明](docs/api.md)。运行证据与研究原始资料留在本机，不随源码上传。

## 文档入口

- [AGPL 深度融合实施计划](PLAN.md)：唯一长期计划、阶段、验收、风险与防跑偏规则。
- [AGPL 深度融合目标规格](docs/specs/2026-10-27-agpl-fusion-target.md)：两层记忆、单 MCP/Skill/Hook、顺序任务与跨客户端目标行为。
- [文档总览](docs/README.md)：当前有效文档的职责与入口。
- [Agent 安装入口](AGENT_INSTALL.md)：由已有 Agent 准备环境和接入资源。
- [手动安装](docs/manual-install.md)：逐项配置与更新。
- [Agent 使用指引](docs/agent-guide.md)：任务与知识维护循环。
- [产品行为基准](docs/specs/2026-10-27-product-realignment.md)：融合前四时点行为、只读入口与验收边界，用于迁移和历史对照。
- 本机开发区的 `context/CURRENT_STATE.md` 保存接续状态；`docs/history/` 保存原始讨论、v0.1 原文和合并后的开发历程；当前 active 记忆库是 `%USERPROFILE%\.memweave`。以上本机资料均不随源码分发。

## 已确认方向

| 项目 | 选择 |
|---|---|
| 许可证 | `AGPL-3.0-or-later`，完全开源；`LICENSE` 与第三方声明已落地 |
| 核心语言 | Python |
| 首批客户端 | Codex 桌面版、Cursor |
| 产品形态 | 单 MCP + 单 Skill + 单 Hook 入口，辅以本地 CLI |
| 融合底座 | 直接 fork/改编 Basic Memory；移植 planning-with-files 生命周期 |
| 存储 | Markdown 权威，SQLite 派生索引 |
| 任务模型 | 同项目单 current 顶层任务，其余顺序排队；任务内 WorkUnit 可并行 |
| 图谱 | 文件结构、项目架构关系作为支撑证据，不是独立记忆层 |
| 理解与提炼 | 由宿主 Agent 完成，首版不独立调用模型 |
| 核心收益 | 工作记忆提升任务可靠性，长期积累改善合作体验 |

## 开发区

当前文档在 `docs/`，本机历史在 `docs/history/`，接续状态在 `context/CURRENT_STATE.md`。产品记忆库已迁出开发区到 `%USERPROFILE%\.memweave`；`work/` 只保留当前验证证据，旧路径、旧版本环境、一次性脚本与重复日志不再长期堆积。历史讨论中的较早建议可能已被后续决策取代，当前方向以状态文档和现有正式文档为准。
