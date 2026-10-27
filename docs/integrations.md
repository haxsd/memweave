# 客户端接入与验证

0.2.4 提供 Codex 与 Cursor 接入。Agent 自动安装见 [仓库安装入口](../AGENT_INSTALL.md)，逐项操作见 [手动安装](manual-install.md)。管理页是可选辅助，配置、协议、原生 CLI、桌面体验与模型收益分别记录。

## Cursor 安装、更新与检查

安装当前 Python 包后执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --root 'D:\my-project' --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave check --root 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave check --root 'D:\my-project' --probe
```

首次 `setup` 合并项目 `.cursor/mcp.json` 和 `hooks.json`，安装包内 Skill 与常驻 Rule，初始化记忆库并登记精确根目录。不修改全局设置，不创建业务任务或扫描。现有 `memweave-trial.mdc` 可作为外部规则保留，否则生成 `rules/memweave.mdc`。

`.cursor/memweave.json` 记录版本、解释器、记忆库、工具集、受管文件哈希与 Hook 条目。再次运行是更新或修复，不重复添加 Hook。省略 `--home` 时沿用清单；首次接入省略时使用用户目录 `.memweave`。`--root` 默认当前目录。

更新 Python 包后运行 `setup` 同步资源，不自动下载新版包或更新 Python。修改前验证配置；受管 Skill/Rule 有自定义改动时拒绝覆盖，提示合并，缺失文件可修复。旧文件备份至 `.cursor/memweave-backups/`，逐文件原子替换；运行时写入失败会尝试恢复本次写入，外部并发修改会保留。此机制不是断电下的跨文件事务。拒绝链接和 junction 配置路径。

`check` 默认只读，检查版本、解释器路径、服务命令、MCP、Hooks、资源哈希、记忆库与项目登记；发现问题返回非零退出码。`--probe` 在配置检查通过后启动与当前安装环境匹配的服务，读取工具目录、状态和工作区；不写业务任务、不扫描。服务启动仍执行既有初始化与中断写入恢复。自动化可加 `--json`。

接入或更新后，在现有 Cursor 窗口中重启 MemWeave MCP 服务并核对工具目录。命令不启动 Cursor；配置和协议检查无法证明当前 Cursor 已重新加载或自动触发 Hooks。

## 工具集

默认 `core` 提供 20 个具名工具。移出默认目录的七项为 `probe_write`、`probe_read`、`recovery_status`、`recover`、`recovery_resolve`、`project_list`、`context_get`。项目与上下文统一使用 `workspace_prepare`，任务列表保留以支持截断后的选择。

需要旧接口时使用 `setup --root <目录> --toolset full`，或给 `serve/config` 加 `--toolset full`，保留原 27 个工具。探针 CLI 自动启动 full 服务；业务 CLI action 不变。收敛减少工具定义体积，尚未测量 Cursor 的实际 token 消耗或任务效果。

## 初始化并生成配置

以下为手工接入和 Codex 配置生成路径。先按 [开发说明](development.md) 安装当前版本，用同一个虚拟环境初始化记忆库并生成配置：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave --version
& '.\.venv\Scripts\python.exe' -m memweave init --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --home '.\work\runtime\memory home'
```

所有客户端配置必须指向同一个 `--home`。命令只输出配置，不写用户全局文件。启动命令使用当前解释器的绝对路径和独立参数数组，因此不会依赖客户端 PATH 或工作目录。移动项目或虚拟环境后需要重新生成。

`work/runtime/client-configs/` 保留此前生成的本机示例；解释器、记忆库路径和所加载版本都应重新核对，不能把它当作新包已安装或客户端已验收的证据。

接入后先读取 `memweave_system_status`，再调用 `memweave_workspace_prepare`，提供精确的绝对根目录与 `inspect`、`new` 或 `resume` 意图。未注册时显式注册；多个活跃任务返回 `task_selection_required` 时明确选择，不按最近时间猜测。用户范围知识只在显式设置 `include_user=true` 时纳入。完整顺序见 [Agent 内使用指引](agent-guide.md)。

## Cursor

MCP 输出为 `mcpServers.memweave` JSON 配置，可将这个条目合入目标项目的 `.cursor/mcp.json` 或用户已有 MCP 配置，保留其他服务。是否加载成功须在目标 Cursor 版本内核对工具列表和实际调用。

生成 Hook 配置：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --kind hooks --home '.\work\runtime\memory home'
```

Hook 输出为 `version: 1` 的配置，合入项目 `.cursor/hooks.json` 时保留其他 Hook。Windows 命令按 PowerShell 字面参数引用生成。0.2.2 生成六类事件：

| 事件 | 实际行为 |
|---|---|
| `sessionStart` | 唯一、已注册的精确根目录下，通过 `additional_context` 返回项目知识、有效结构与入口指引。使用 `new` 上下文，不选旧任务、不含用户范围知识、不自动注册或扫描 |
| `afterMCPExecution` | 只观察名为 `memweave` 的 MCP 服务成功的任务创建、工作区准备、检查点与关闭结果；核对实际任务归属后关联当前会话 |
| `afterFileEdit` | 已关联任务且编辑路径在项目根目录内时，标记需要检查点；不保存文件内容 |
| `stop` | `status=completed`、`loop_count=0`、有 generation ID 且已关联任务时，针对未保存的工作或已完成任务的待提炼状态返回一次 `followup_message`。配置 `loop_limit:1`，本地状态也抑制同一代重复提示 |
| `sessionEnd` | 记录事件并删除该会话的最小运行状态，不影响其他会话 |
| `preCompact` | 只记录事件。官方协议不允许此事件阻止或修改压缩，不把它当作已保存语义总结 |

启动上下文预算为 3500 字符，加上固定指引和覆盖概况。多个工作区根、未知根或缺少有效根目录时不注入。Hook 不自动恢复唯一活跃任务；宿主仍须按用户意图调用 `workspace_prepare`。`new/inspect` 准备会清除当前会话的任务关联，明确的 `resume` 成功后才建立关联。检查点保存成功后清除待记标记，之后的项目文件编辑可重新标记。知识提炼仍需宿主实际调用；没有可复用知识时可以明确记录空结果。

跟进提示不会替宿主写检查点、关闭任务或生成知识。取消、错误、自动续轮、缺少任务归属和已完成提炼时不追加。Skill 仍负责重要发现、失败、验证和阶段变化的持续记录；Hook 无法从文件编辑之外的全部语义活动判断是否遗漏。若将 MCP 服务改名，需同步适配观察器；当前生成配置使用固定名称 `memweave`。

事件输入最大 65,536 字节，需为 UTF-8 JSON 对象，并有 `conversation_id` 或 `session_id`。只保留 `conversation_id`、`session_id`、`generation_id`、`event_id` 中存在的字符串标识；忽略 prompt、transcript 等字段。相同客户端、事件及这些标识组成的收据去重。没有更细的事件标识时，相同 session/generation 内重复事件会合并，这只适用于本阶段的兼容性观察，不能直接用作后续业务调度。

除收据之外，SQLite `hook_sessions` 保存会话根目录、实际任务 ID、版本、待检查点标记和已提示代次，作为短期运行状态；不保存 prompt、完整 tool_input/result_json、文件编辑正文或 transcript。`sessionEnd` 清除此状态，收据保留。未收到结束事件的会话状态会继续保留，当前没有自动过期清理。

输入错误、存储错误及不支持的事件写入 stderr，输出 `{}` 并成功退出，使宿主继续原任务。收据来源标为 `hook_entrypoint`：手动执行也能产生收据，因此收据本身不能证明 Cursor 自动触发成功。`afterMCPExecution` 支持业务 JSON、MCP `structuredContent` 和单个文本内容块；未知或错误响应不建立任务关联。事件输入上限仍为 64 KiB，超大 MCP 结果会跳过而不阻止原工作。

## Codex 接入与 Hooks

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root 'D:\my-project' --home '<共享记忆库绝对路径>'
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root 'D:\my-project' --probe
```

安装合并 `.codex/config.toml` 中的 MCP 与 `[features] hooks = true`，保留 TOML 注释、模型、其他服务和设置；合并 `.codex/hooks.json`，安装 `.agents/skills/memweave-continuity/SKILL.md`。不修改根目录 `AGENTS.md` 或全局配置。清单位于 `.codex/memweave.json`，备份位于 `.codex/memweave-backups/`。生成局部 `.gitignore`，使本机路径和受管资源保持本地；已经被 Git 跟踪的文件仍须自行核对。

项目 `.codex/` 需要被 Codex 信任。非托管 Hooks 另需用户审查并信任当前定义哈希，支持此能力的 CLI 使用 `/hooks`；定义变化后重新审查。`check` 始终返回 `hook_trust:requires_host_review`，不会假造已信任状态或代写宿主信任库。

| 事件 | 实际作用 |
|---|---|
| SessionStart | 注入有预算的项目背景；startup/clear/fork 不选旧任务，resume/compact 恢复本会话此前实际关联的任务 |
| UserPromptSubmit | 注入简短的工作区和归属提示，不保存用户提示词 |
| PostToolUse | 观察成功的本服务任务调用以建立关联；项目内补丁和可识别验证命令标记待保存，失败验证也算新证据 |
| Stop | 对明确任务的未保存进展或待提炼状态请求一次宿主续轮；确有改动/验证而无任务时，要求按真实目标准备和记录 |
| Interrupt | 标记用户中断，不通过 Stop 重新启动同一轮 |
| PreCompact | 仅保留事件观察，不伪造压缩总结或阻止压缩 |
| SessionEnd | 清理本会话最小状态 |

Codex 使用官方 `hookSpecificOutput.additionalContext` 与 Stop `decision:block/reason` 协议，`stop_hook_active` 和 turn ID 限制续轮。Hook 不理解补丁语义、不读取 transcript、不保存命令或输出、不代替 MCP 写入；宿主依 Skill 完成真实记录。仅识别部分补丁和验证工具，任意 shell 写文件或纯语义发现仍需宿主主动检查点。

Windows `commandWindows` 显式启动 PowerShell，以编码命令保护中文、空格和单引号参数；编码不用于存放秘密。输入仍为 64 KiB 上限；超大事件跳过且不阻止原任务。收据只保存标识字段，Codex 另包括 `turn_id/tool_use_id`，会话状态按宿主隔离。

本机 Codex CLI `0.164.0-alpha.11.1` 的 Hooks 开关为 stable/enabled；2026-10-27 已读取 [官方 Hooks](https://developers.openai.com/codex/hooks) 与 [配置参考](https://developers.openai.com/codex/config-reference/)。原生 CLI 受控 provider 已验证实际事件、MCP 调用、Stop 续轮和检查点落盘；这是集成证据，不等于桌面界面验收或真实模型收益。普通安装不会跳过 Hook 信任。

## 复制原创 Skill

源码中的 `src/memweave/skills/memweave-continuity/` 是完整 Skill 目录，入口为 `SKILL.md`。安装包内对应资源为 `memweave/skills/memweave-continuity/`。Codex / Cursor `setup` 负责项目级安装；手工接入时复制整个目录并核对识别结果，不自动修改全局 Skill 目录。

文案遵循 [Agent 内使用指引](agent-guide.md) 的四时点流程。复制和识别只证明资源可用；是否适时触发、是否正确调用，以及是否减少遗漏或重复探索，需要真实宿主观察和公平模型对照。

## 目标客户端的验收操作

分别在 Codex 桌面版与 Cursor 中执行，并记录客户端版本、时间、实际工具调用、结果和额外操作负担。同 IDE 的后续工作与跨客户端接续都应观察。

1. 加载服务，确认 `memweave_*` 工具出现，包含 `workspace_prepare`、task、memory、graph 工具；读取状态并核对版本与 `--home`。
2. **开始或恢复：** 对精确根目录执行 `workspace_prepare`。验证未注册目录不被偷偷创建、`new` 不恢复旧任务、多个活跃任务需要明确选择。新任务写明目标与验收条件。
3. **执行：** 在真实重要发现、失败和验证后保存检查点；重启会话或切换客户端后恢复，核对最新下一步、已启用知识和有效局部结构与当前代码是否一致。
4. **阶段结束或完成：** 逐项记录验收结果；由宿主完成提炼与去重，不确定内容保留候选。检查无宿主时仍为待提炼，而不是把服务或事件收据当成完成。
5. **下一次相关工作：** 观察宿主是否在相关问题中取回并实际使用知识与结构；纠正后重新检索，并在受控记录上验证遗忘。核对项目隔离、用户范围显式选择以及变化来源的排除。
6. 在对应客户端触发会话开始、实际 MCP 调用、文件编辑和正常停止，结合客户端日志、启动上下文、一次跟进和最终 MCP 写入核对。取消时不续跑，跟进后不循环。单独执行 Hook CLI、SDK 或 Codex 原生 CLI 不算桌面界面自动验收；Codex 先完成项目与 Hook 信任。

协议进程通过、配置生成、Skill 复制、事件收据、页面演示与真实桌面四时点流程是不同证据。当前两个客户端的 `desktop_verification` 仍为 `unverified`；服务的 `phase:local_mvp` 表示工程阶段，不能用来判定产品体验已完成。

工程流程通过后，再对照无记忆、仅任务记忆、完整记忆与图谱，评价遗漏、重复探索、重复说明、纠正成本和额外开销。没有这类证据时，不宣称模型行为收益。

## 可选诊断与管理页

`memweave_probe_write` 与 `memweave_probe_read` 可在两个客户端或重启前后读写同一诊断标记，验证基本连通性。相同操作 ID 与内容应返回原结果，改变内容复用 ID 应冲突。标记不能替代任务上下文。

需要辅助查看、纠正或遗忘内容时，可运行 `memweave ui --home <同一个记忆库>`，或使用根目录 `start.cmd`。它不会代替宿主执行提炼，且不是接入 MCP 的前置步骤。

## 本轮资料

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 和 [客户端文档](https://py.sdk.modelcontextprotocol.io/client/)：本轮成功读取，实际依赖锁定 2.3.0。
- [Cursor MCP](https://cursor.com/docs/context/mcp) 与 [Hooks](https://cursor.com/docs/agent/hooks)：本轮成功读取，含 Windows Hooks 经 PowerShell 调用的说明。
- [Codex Hooks](https://developers.openai.com/codex/hooks) 与 [配置参考](https://developers.openai.com/codex/config-reference/)：2026-10-27 成功读取；此前 403 是历史获取结果。
