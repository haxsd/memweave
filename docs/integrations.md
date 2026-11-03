# 客户端接入与验证

> 状态：本文描述 **当前开发接入**。主 MCP 使用 Basic Memory 单索引和稳定记忆库 `%USERPROFILE%\.memweave`；顶层任务为单 `current` 顺序队列，任务内 WorkUnit 可并行，阶段可独立收束。Phase 12 将 Hook 统一为低频会话生命周期；2026-11-02 起，Codex 只额外注册 `PreCompact`（压缩前有界快照）和 `UserPromptSubmit`（仅在快照 pending 时注入；普通输入快速返回）以补齐真实压缩恢复，Cursor 仍只生成 `sessionStart/sessionEnd`。文件编辑、工具调用、逐轮原话检索和 Stop 维护仍走常驻 MCP；其它历史高频事件只代表旧配置。Codex CLI/桌面已产生 `SessionStart`/`SessionEnd` 收据；Cursor `sessionStart` 是 fire-and-forget，3.27.22 实测没有把 `additional_context` 送入模型上下文，真实恢复依赖首轮 `workspace_prepare`。Codex 压缩 Hook 的客户端重载和 Cursor `sessionEnd` 仍待验证。目标见 [目标规格](specs/2026-10-27-agpl-fusion-target.md)，执行顺序见 [PLAN.md](../PLAN.md)。

0.2.5 提供 Codex 与 Cursor 接入。Agent 自动安装见 [仓库安装入口](../AGENT_INSTALL.md)，逐项操作见 [手动安装](manual-install.md)。管理页是可选辅助，现已支持项目状态和架构维护；配置、协议、原生 CLI、桌面体验与模型收益仍分别记录。

## 全机接入巡检与修复

单项目 `check` 只回答一个显式目录是否健康；需要核对“本机所有已知接入是否仍指向同一个活动库”时执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave clients doctor
& '.\.venv\Scripts\python.exe' -m memweave clients doctor --json
& '.\.venv\Scripts\python.exe' -m memweave clients doctor --fix
```

默认扫描活动库登记的项目根目录、这些仓库的 linked worktree，并补充 Codex 全局配置中的已知可信项目；`--root <目录>` 可重复追加未登记或 Cursor 侧已知项目，`--client cursor|codex` 可限定一侧。巡检同时查看全局入口与项目级安装，稳定报告 `stale_home`、`version_mismatch`、`interpreter_missing`、`server_mismatch`、`mcp_mismatch`、`hooks_missing`、`skill_drift`、`rule_drift`、`local_ignore_missing`、`manifest_drift`、`project_unregistered`、`worktree_unconfigured`、`duplicate_mcp`、`config_invalid`、`root_invalid` 等问题。

默认只读。`--fix` 只对存在问题的项目级受管安装复用 `setup`：保留配置备份，继续拒绝覆盖被人工修改的受管 Skill/Rule；健康目标不会写文件。全局客户端入口和无法安全合并的配置只报告，不擅自改写。修复后相关目标标记 `repaired` 和 `restart_required`，必须在已有客户端重载 MemWeave 后重新运行巡检。存在未解决项时退出码为 1；`not_installed` 只是事实，不是修复失败。

## Cursor 安装、更新与检查

安装当前 Python 包后执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --root 'D:\my-project' --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave check --root 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave check --root 'D:\my-project' --probe
```

首次 `setup` 合并项目 `.cursor/mcp.json` 和 `hooks.json`，安装包内 Skill 与常驻 Rule，初始化记忆库并登记项目。它会在 `.cursor/.gitignore` 中合并本机受管文件和备份目录的忽略规则，避免接入配置污染用户仓库，同时保留已有内容。主 checkout 与同一 Git 仓库的 linked worktree 解析为同一 project；从 worktree 执行 setup 时，受管客户端文件仍写在该执行根，但不会新增第二个项目。命令不修改全局设置，不创建业务任务或扫描。现有 `memweave-trial.mdc` 可作为外部规则保留，否则生成 `rules/memweave.mdc`。

Cursor 的项目 Hook 按工作区的项目根目录解析。MemWeave 当前只支持一个工作区根：请用 **File → Open Folder** 直接打开目标项目，不要用 **Add Folders** 把它并入另一个工作区或测试夹具。多个工作区根时 Hook 会保持沉默，Cursor 也可能只读取第一个根的 `.cursor/hooks.json`。2026-10-28 的本机日志已复现：`D:\project` 被并入测试夹具工作区后，Cursor 加载的是夹具 Hook，真实项目没有形成 MemWeave 任务。

`.cursor/mcp.json` 中存在服务条目不等于当前 Agent 会话已经加载它。Cursor 可能需要在当前会话的 MCP/工具入口中手动启用或连接 `memweave`；只有工具列表出现 `memweave_*` 后才能视为可用。配置检查、`clients doctor` 和 `check --probe` 都不能替代这一桌面步骤。

`.cursor/memweave.json` 记录版本、解释器、记忆库、工具集、受管文件哈希与 Hook 条目。再次运行是更新或修复，不重复添加 Hook。省略 `--home` 时沿用清单；首次接入省略时使用用户目录 `.memweave`。`--root` 默认当前目录。

更新 Python 包后运行 `setup` 同步资源，不自动下载新版包或更新 Python。修改前验证配置；受管 Skill/Rule 有自定义改动时拒绝覆盖，提示合并，缺失文件可修复。旧文件备份至 `.cursor/memweave-backups/`，逐文件原子替换；运行时写入失败会尝试恢复本次写入，外部并发修改会保留。此机制不是断电下的跨文件事务。拒绝链接和 junction 配置路径。

`check` 默认只读，检查版本、解释器路径、服务命令、MCP、Hooks、资源哈希、记忆库与项目登记；发现问题返回非零退出码。`--probe` 在配置检查通过后启动与当前安装环境匹配的服务，读取工具目录、状态和工作区；不写业务任务、不扫描。服务启动仍执行既有初始化与中断写入恢复。自动化可加 `--json`。

接入或更新后，在现有 Cursor 窗口中重启 MemWeave MCP 服务并核对工具目录。命令不启动 Cursor；配置和协议检查无法证明当前 Cursor 已重新加载、Hooks 已触发，更不能证明模型收到启动上下文。真实场景 3 已证明 Cursor `3.27.14` Agents 可通过 MCP 推进共享任务。Cursor `3.27.22` 的 `D:\project` 有单根工作区 Hook 输出与 MCP 调用历史，`github-radar` 随后完成真实任务、关闭和知识审查；Phase 14 在普通首轮和检查点后新会话中验证了首轮 `workspace_prepare` 恢复，但同版本模型没有收到 `sessionStart` 块。历史上 `stop`、`afterFileEdit` 与 `afterMCPExecution` 均执行过，但这些逐轮/逐工具事件已在 Phase 12 从默认安装移除。升级后必须重新运行 setup 并重载客户端，旧进程中已加载的高频配置才会消失。一条 `MainThreadShellExec not initialized` 是客户端结束事件执行失败，不是配置漏装。Codex CLI/桌面已产生成功 `sessionEnd` 样本；Cursor 原生 Agent 已产生 `sessionStart` 输出；Cursor `sessionEnd` 与 `preCompact`（未安装）仍无真实样本。该版本遇到同名全局服务时会使用全局项并遮蔽项目级配置；两者应指向同一记忆库，或只保留一个同名项。

`memweave clients doctor` 会比较受管 Skill/Rule 与当前包内资源，而不是只比较安装清单里的旧哈希；因此源码升级后，过期但仍“未手改”的 Cursor 资源也会进入 `needs_attention`，可由 `--fix` 更新并保留备份。

## 通用工具面

日常 Agent 只连接一个 22 工具的通用目录。项目、队列概览与上下文统一使用 `workspace_prepare`；知识检索结果直接带回 revision/etag；任务工具负责队列、WorkUnit 与阶段，知识/架构工具负责检索、写入和纠正。诊断探针、项目列表、完整上下文读取、架构遗忘和恢复动作不在 Agent 工具面中，分别走 CLI、内部探针或管理页。

不提供 `core/full` 或轻量/全量选择。旧的 `--toolset` 输入只作为迁移兼容被忽略；`setup`/`clients doctor --fix` 会更新受管配置，客户端重载后只剩通用工具面。`memweave probe` 使用内部诊断路径验证 stdio 和重启读回，不改变用户可见目录。

## 初始化并生成配置

以下为手工接入和 Codex 配置生成路径。先按 [开发说明](development.md) 安装当前版本，用同一个虚拟环境初始化记忆库并生成配置：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave --version
& '.\.venv\Scripts\python.exe' -m memweave init --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --home "$HOME\.memweave"
```

所有客户端配置必须指向同一个 `--home`。命令只输出配置，不写用户全局文件。启动命令使用当前解释器的绝对路径和独立参数数组，因此不会依赖客户端 PATH 或工作目录。移动项目或虚拟环境后需要重新生成。

生成配置应指向当前安装解释器、当前版本和同一个绝对记忆库。项目内 `.codex/`、`.cursor/` 属于本机接入状态，不随源码分发；`check`、协议自检和客户端实际加载必须分别记录。

接入后先读取 `memweave_system_status`，再调用 `memweave_workspace_prepare`，提供当前绝对执行根与 `inspect`、`new` 或 `resume` 意图。子目录和不同 clone 不自动归并；同一 Git common directory 的 linked worktree 会复用已登记项目。未注册时显式注册；resume 只选唯一 current，queued 任务按确定顺序等待。优先读取 `context.reboot` 的五问摘要，并处理 `attention` 中的 warning；`completion.state=ready` 只表示没有打开的 WorkUnit，不是验收通过。并行子代理在 current 任务内使用独立 WorkUnit，不创建多个 current。WorkUnit 的独立 revision/etag 只保护记录，不隔离工作树；并行改文件时应使用独立 worktree 或互不重叠的文件范围，由父 Agent 合并验收。用户范围知识只在显式设置 `include_user=true` 时纳入。完整顺序见 [Agent 内使用指引](agent-guide.md)。

## Cursor

MCP 输出为 `mcpServers.memweave` JSON 配置，可将这个条目合入目标项目的 `.cursor/mcp.json` 或用户已有 MCP 配置，保留其他服务。是否加载成功须在目标 Cursor 版本内核对工具列表和实际调用。

生成 Hook 配置：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --kind hooks --home "$HOME\.memweave"
```

Hook 输出为 `version: 1` 的配置，合入项目 `.cursor/hooks.json` 时保留其他 Hook。Windows 命令按 PowerShell 字面参数引用生成。当前只生成两类低频会话事件：

| 事件 | 实际行为 |
|---|---|
| `sessionStart` | 当前绝对执行根属于唯一已登记项目时，通过 `additional_context` 尝试返回 current 任务、打开 WorkUnit、项目知识、有效结构与入口指引；同一仓库的 linked worktree 也接受。queued 任务不会自动抢占。Cursor 官方契约是 fire-and-forget；3.27.22 实测输出未进入模型上下文，因此必须按 best effort 处理并在首轮使用常驻 MCP 恢复。不含用户范围知识，不创建任务或扫描 |
| `sessionEnd` | 记录事件并删除该会话的最小运行状态，不影响其他会话 |

启动上下文预算为 3300 字符，加上固定指引、任务计数和覆盖概况。多个工作区根、未知根或缺少有效根目录时不注入。唯一 current 任务自动成为恢复候选；宿主必须先核对当前用户目标，若目标不同则显式使用 `new`。queued 任务不会被自动选中。待审查任务、阻塞 WorkUnit 和待审查阶段在下一次 SessionStart 或显式 MCP 读取时展示，不通过逐轮 Stop 打断当前请求。阶段和任务提炼仍需宿主在自然边界实际调用；没有可复用知识时可以静默记录空结果。

Skill 负责重要发现、失败、验证和阶段变化的持续记录；Hook 不从文件编辑或工具结果推断“待检查点”，也不生成跟进提示。普通原话检索由 Agent 通过常驻 MCP 的 `workspace_prepare` 或 `memory_search` 完成；Codex `UserPromptSubmit` 仅消费 pending 压缩快照，不注册 `afterMCPExecution`。恢复或新建任务后，宿主应在面向用户的第一条回复显示一行 `MemWeave · 任务 <id> · <next_step>`，没有任务时显示 `MemWeave · 尚未建立任务`。该要求同时写入常驻 Rule、Hook 上下文和 continuity Skill。默认 Rule、Skill 与 MCP 指令还要求同一用户请求内的普通步骤连续执行；不因完成子步骤或保存检查点而等待批准。客户端自身的回合边界仍由宿主控制，不提供强制续跑的 Stop Hook。

事件输入最大 65,536 字节，需为 UTF-8 JSON 对象，并有 `conversation_id` 或 `session_id`。收据只保留 `conversation_id`、`session_id`、`generation_id`、`event_id`、`turn_id` 等允许的标识；Cursor 的 `sessionStart` 会发送空 `generation_id`，因此空的可选标识按缺失处理，不能阻断 Hook。不会读取 transcript。相同客户端、事件及允许标识组成的收据去重。

父项目 Hook 若发现当前工作目录位于一个带独立 `.codex/memweave.json` 的嵌套项目内，会保持沉默，避免父项目和子项目同时注入两套话题。没有独立安装清单的手工嵌套配置不做此保证。

除收据之外，SQLite `hook_sessions` 保存会话根目录、最近定位到的任务 ID、版本和已提示代次，作为短期运行状态；不保存 prompt、完整 tool_input/result_json、文件编辑正文或 transcript。`sessionEnd` 清除此状态，收据保留。未收到结束事件的会话状态会继续保留，当前没有自动过期清理。

输入错误、存储错误及不支持的事件写入 stderr，输出 `{}` 并成功退出，使宿主继续原任务。收据来源标为 `hook_entrypoint`：手动执行也能产生收据，因此收据本身不能证明 Cursor 自动触发成功。旧版高频事件不再由当前安装生成，重新运行 setup 会从受管清单中移除它们。

## Codex 接入与 Hooks

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root 'D:\my-project' --home '<共享记忆库绝对路径>'
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root 'D:\my-project' --probe
```

安装合并 `.codex/config.toml` 中的 MCP 与 `[features] hooks = true`，保留 TOML 注释、模型、其他服务和设置；合并 `.codex/hooks.json`，安装 `.agents/skills/memweave-continuity/SKILL.md`。不修改根目录 `AGENTS.md` 或全局配置。清单位于 `.codex/memweave.json`，备份位于 `.codex/memweave-backups/`。生成局部 `.gitignore`，使本机路径和受管资源保持本地；已经被 Git 跟踪的文件仍须自行核对。

Codex 侧栏把线程拖入一个项目只会改变分组，不会重写已有线程的 `cwd`、MCP 连接或 Hook。若线程实际运行在 linked worktree，必须在那个执行根安装受管入口；主 checkout 的 `.codex/` 不会被 Git worktree 自动继承。运行 `memweave clients doctor` 会发现 `worktree_unconfigured`，`--fix` 在该 worktree 写入 `.codex/` 与 `.agents/skills/`，但项目登记仍解析到主 checkout；完成后从该 worktree 新建会话或重载 MCP。配置检查不等于旧线程已经热加载。

项目 `.codex/` 需要被 Codex 信任。非托管 Hooks 另需用户审查并信任当前定义哈希，支持此能力的 CLI 使用 `/hooks`；定义变化后重新审查。`check` 始终返回 `hook_trust:requires_host_review`，不会假造已信任状态或代写宿主信任库。

Codex 项目 MCP 设置 `required = true` 与 `startup_readiness = "catalog"`，要求以工具目录作为启动就绪条件；服务初始化失败时显式报错。当前本机 Codex `0.166.0-alpha.5.1` 解析器支持该配置；旧运行中的会话需重载后验证。项目安装保留全局配置。配置与子进程探针通过仍不等于模型已拿到工具。

| 事件 | 实际作用 |
|---|---|
| SessionStart | 注入有预算的项目背景；唯一 current 任务、运行中/阻塞 WorkUnit 自动成为恢复候选，queued 不抢占。startup/resume/compact 都先核对用户目标，不把旧进度强制带入新目标 |
| PreCompact | 读取 Codex transcript 尾部，保存当前任务、最近用户/助手文本和工具名称的有界快照；不保存命令、工具参数、transcript 路径、完整对话或工具输出，不阻断压缩 |
| UserPromptSubmit | 仅在 `PreCompact` 快照 pending 时注入恢复包；无 pending 时由快速路径直接返回，不检索、不写逐轮 receipt |
| Interrupt | 标记用户中断，不通过 Stop 重新启动同一轮 |
| SessionEnd | 清理本会话最小状态 |

Codex 使用 `hookSpecificOutput.additionalContext`。默认不注册 Stop，因此不会插入额外续轮。`PreCompact` 只读取 transcript 尾部并保存有界摘要，同时写入 current 任务检查点；未绑定或旧任务已结束时通过现有项目与任务存储重新查找 current，捕获与消费均不启动搜索索引。`UserPromptSubmit` 无 pending 时直接返回。恢复块在下一次用户输入或 `SessionStart(compact)` 时消费一次，同回合自动续跑是否收到注入仍取决于宿主事件。Hook 会持久化有界用户/助手文本摘录，不能据此保证任意自然语言中的敏感信息都被过滤；命令、工具参数和输出不会进入新快照。宿主仍须在重要发现、用户纠正和验证后主动更新检查点。普通原话检索通过常驻 MCP 完成，不注册 `PostToolUse`。

Windows `commandWindows` 显式启动 PowerShell，以编码命令保护中文、空格和单引号参数；编码不用于存放秘密。输入仍为 64 KiB 上限；超大事件跳过且不阻止原任务。收据只保存标识字段，Codex 另包括 `turn_id/tool_use_id`，会话状态按宿主隔离。

本机 Codex CLI `0.166.0-alpha.5.1` 的 Hooks 开关为 stable/enabled；2026-10-27 已读取 [官方 Hooks](https://developers.openai.com/codex/hooks) 与 [配置参考](https://developers.openai.com/codex/config-reference/)。真实 Codex 线程的 `contextCompaction` 未触发旧的 `SessionStart(compact)` 路径，因此当前实现改用 `PreCompact` 保存快照、`UserPromptSubmit` 按需恢复；历史收据只说明旧配置确实触发过，不代表当前安装。普通安装不会跳过 Hook 信任，定义变化后需重新审查。

## 复制原创 Skill

源码中的 `src/memweave/skills/memweave-continuity/` 是完整 Skill 目录，入口为 `SKILL.md`。安装包内对应资源为 `memweave/skills/memweave-continuity/`。Codex / Cursor `setup` 负责项目级安装；手工接入时复制整个目录并核对识别结果，不自动修改全局 Skill 目录。

文案遵循 [Agent 内使用指引](agent-guide.md) 的四时点流程。复制和识别只证明资源可用；是否适时触发、是否正确调用，以及是否减少遗漏或重复探索，需要真实宿主观察和公平模型对照。

## 目标客户端的验收操作

Codex 恢复线程时存在客户端启动边界：日志可能先出现 `turn/start`，再出现 MemWeave MCP ready。服务已把重型索引推迟到首次工具调用，冷启动工具目录实测约 1.34–1.37 秒，但客户端仍可能先开始回合。此时不要让 Agent 凭文案发出 `memweave_workspace_prepare`；只有当前工具列表出现 `memweave_*` 才调用，否则直接说明 MCP 正在加载/重连，等工具出现后再发。把这个协议错误当作客户端启动竞态处理，不要用文件或 CLI 猜测任务状态。

分别在 Codex 桌面版与 Cursor 中执行，并记录客户端版本、时间、实际工具调用、结果和额外操作负担。同 IDE 的后续工作与跨客户端接续都应观察。

1. 加载服务，确认 `memweave_*` 工具出现，包含 `workspace_prepare`、task、memory、graph 工具；读取状态并核对版本与 `--home`。
2. **开始或恢复：** 对当前绝对执行根执行 `workspace_prepare`。验证未注册目录不被悄悄创建、linked worktree 复用同一项目、`new` 不恢复旧任务、queued 任务不会抢占 current。新任务写明目标与验收条件。
3. **执行：** 并行子代理分别更新 WorkUnit；在真实重要发现、失败和验证后保存任务检查点。重启会话或切换客户端后核对最新下一步、WorkUnit、已启用知识和有效局部结构与当前代码是否一致。
4. **阶段结束或完成：** 阶段收束不关闭任务，按 phase_id 做知识审查；任务完成时逐项记录验收，再按队列推进下一项。由宿主完成提炼与去重，不确定内容保留候选。检查无宿主时仍为待提炼，而不是把服务或事件收据当成完成。
5. **下一次相关工作：** 观察宿主是否在相关问题中取回并实际使用知识与结构；纠正后重新检索，并在受控记录上验证遗忘。核对项目隔离、用户范围显式选择以及变化来源的排除。
6. 在对应客户端触发会话开始、实际 MCP 调用、文件编辑和正常停止，结合客户端日志、启动上下文、一次跟进和最终 MCP 写入核对。取消时不续跑，跟进后不循环。单独执行 Hook CLI、SDK 或 Codex 原生 CLI 不算桌面界面自动验收；Codex 先完成项目与 Hook 信任。

协议进程通过、配置生成、Skill 复制、事件收据、页面演示与真实桌面四时点流程是不同证据。Cursor `3.27.14` 在场景 3 通过真实桌面 Agents 与 MCP 完成跨客户端接续；2026-10-28 用户重启 Codex 桌面后，当前代码启动的 MCP 读取同一任务。Cursor `3.27.22` 的 `D:\project` 有 `sessionStart` 输出、可见状态行和任务/检查点历史；`github-radar` 已观察顺序任务关闭、知识审查和旧版 Stop。Phase 14 明确：同一版本的新会话启动块未进入模型上下文，普通首轮与检查点后恢复是通过常驻 MCP 的 `workspace_prepare` 实测的。这些逐轮/逐工具 Hook 样本属于 Phase 12 之前；当前安装需要重新 setup 并重载客户端后，才按新的会话启动/结束事件面观察。Codex CLI/桌面已产生 `sessionEnd` 收据；Cursor 原生 Agent 已产生 `sessionStart` 输出；Cursor `sessionEnd` 与 `preCompact`（未安装）仍缺样本，完整自动结束/压缩、更长会话持续体验和统计收益仍未验证；服务的阶段标签不能用来判定这些能力已完成。

工程流程通过后，再对照无记忆、仅任务记忆、完整记忆，评价遗漏、重复探索、重复说明、纠正成本和额外开销。2026-10-28 的 Codex CLI 单轮对照中，完整记忆取回了隐藏范围策略，任务记忆未取回；但完整记忆耗时和 token 更高，因此只能报告知识召回证据，不能宣称效率收益。该对照不含 Cursor，也不是统计 benchmark。

## 可选诊断与管理页

`memweave_probe_write` 与 `memweave_probe_read` 可在两个客户端或重启前后读写同一诊断标记，验证基本连通性。相同操作 ID 与内容应返回原结果，改变内容复用 ID 应冲突。标记不能替代任务上下文。

需要辅助查看、纠正或遗忘内容时，可运行 `memweave ui --home <同一个记忆库>`，或使用根目录 `start.cmd`。工作总览同时给出当前项目与全局的 knowledge scope/status 和 architecture stale 计数；项目图谱包含 stale 架构及其 revision、etag、来源、关系和原因。纠正来源漂移的架构时必须显式重新扫描并同步当前哈希，遗忘必须输入确认词并使用 tombstone。它不会代替宿主执行提炼，且不是接入 MCP 的前置步骤。

## 本轮资料

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 和 [客户端文档](https://py.sdk.modelcontextprotocol.io/client/)：本轮成功读取，实际依赖锁定 2.3.0。
- [Cursor MCP](https://cursor.com/docs/context/mcp) 与 [Hooks](https://cursor.com/docs/agent/hooks)：本轮成功读取，含 Windows Hooks 经 PowerShell 调用的说明。
- [Codex Hooks](https://developers.openai.com/codex/hooks) 与 [配置参考](https://developers.openai.com/codex/config-reference/)：2026-10-27 成功读取；此前 403 是历史获取结果。
