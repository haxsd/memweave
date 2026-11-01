# API 与存储

> 状态：本文描述 **当前开发接口**。主 MCP 由 Basic Memory 单一服务生命周期持有 Markdown 存储、SQLite 派生索引和 FTS 检索；任务使用单 `current` 顺序队列，任务内 WorkUnit 可并行，阶段可独立收束。Phase 12 增加有界任务历史、独立检查点/阶段记录和归档语义，并把 Codex/Cursor Hook 统一为低频会话生命周期事件；逐工具、逐编辑和逐轮原话动作使用常驻 MCP。Codex CLI/桌面已产生 `SessionStart`/`SessionEnd` 收据；Cursor 原生 Agent 的 `sessionStart` 会输出 `additional_context`，但官方契约是 fire-and-forget，3.27.22 实测未进入模型上下文，实际恢复依赖首轮 `workspace_prepare`。Cursor `sessionEnd`、`preCompact`（未安装）和统计收益仍未验证。目标见 [目标规格](specs/2026-10-27-agpl-fusion-target.md)，计划见 [PLAN.md](../PLAN.md)。

当前 MCP 只有一个 22 个工具的通用面；下表描述的全部业务 action 仍可通过 CLI 使用。诊断探针、项目列表、完整上下文读取、架构遗忘和恢复入口不暴露给 Agent，分别走 CLI、内部探针或管理页。`workspace_prepare` 获取默认工作集，`memory_search` 返回当前知识版本与 etag；`memory_read`、`context_get`、`project_list`、`graph_forget` 和恢复动作仍保留在 CLI/管理路径。不提供 `core/full` 或轻量/全量选择，旧 `--toolset` 参数只为迁移兼容并被忽略。

Hook 在内部复用同一只读 `workspace_prepare`：会话开始只恢复唯一 `current` 任务、运行中/阻塞 WorkUnit、待审查项和近期归档概况；queued 任务不会抢占。默认不注册逐轮 Stop。当前请求相关的原话检索和任务维护由 Agent 在常驻 MCP 中调用 `workspace_prepare`/`memory_search`；不再注册 `Stop`、`UserPromptSubmit`、`PostToolUse`、`afterMCPExecution` 或 `afterFileEdit`。Hook 不新增 MCP 工具，也不把原话写入业务记录或收据。Cursor 的 `additional_context` 不等于模型已收到；常驻 MCP 可用时，首个正常请求必须先恢复任务，不能等待启动块或要求用户重述。

CLI、MCP 和网页共享 `Service`。CLI `call` 使用下表 action；MCP 使用 `memweave_` 前缀，状态工具名是 `memweave_system_status`。标准成功返回 `{status:"ok",data:...}`，失败为领域错误。MCP 使用协议工具错误，CLI 错误写 stderr 且退出码为 1。

```powershell
$request = @{root='C:\Projects\example'; name='example'; operation_id='register-unique-id'} | ConvertTo-Json
$request | & '.\.venv\Scripts\python.exe' -m memweave call project_register --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave call project_list --input '{}' --home "$HOME\.memweave"
```

CLI 支持 `--input <JSON>` 或 stdin UTF-8 JSON，最大 256 KiB。为避免复杂 JSON 转义，推荐通过 stdin 传对象。Windows PowerShell 5.1 的管道可能将中文转换为问号；批处理中文 JSON 请用 PowerShell 7，或从 Python 的 UTF-8 管道调用，网页和 MCP 无此限制。

`memweave clients doctor [--home HOME] [--root DIR ...] [--client all|cursor|codex] [--fix] [--json]` 是只读 CLI 运维入口，不作为 MCP action。默认根目录集合由活动库已登记项目和 Codex 全局 `projects` 中存在的目录组成；显式 `--root` 追加目录。返回顶层 `state`（`ready|needs_attention|repaired`）、`roots_scanned`、`targets_scanned`、状态计数和逐目标 `targets[]`。目标区分 `global|project` 与 `source=registered|codex_known|explicit`，问题 code 包括 `stale_home`、`version_mismatch`、`server_mismatch`、`interpreter_missing`、`mcp_mismatch`、`hooks_missing`、`skill_drift`、`rule_drift`、`manifest_drift`、`project_unregistered`、`duplicate_mcp`、`config_invalid`、`unmanaged_installation` 和 `root_invalid`。

`--fix` 仅对项目级可修复项调用现有 `setup`，结果包含 `repair.state`、修改文件、备份和 `restart_required`；健康目标不写文件。全局入口没有自动修复路径，`duplicate_mcp` 和受管资源冲突默认要求人工合并。命令的成功/失败退出码分别为 0/1，修复成功但需要客户端重载仍返回 0；`clients doctor` 不启动或重载客户端，不能替代 Hook 注入与宿主使用验收。

| action | 必填参数与主要可选参数 |
|---|---|
| project_register | root, name, operation_id |
| project_list | 无 |
| workspace_prepare | root:存在的绝对项目根目录；intent:inspect/new/resume，默认 inspect；可选 task_id（仅 resume）、query、include_user:false、budget:500–32000 |
| task_open | project_id, title, goal, acceptance: 字符串数组, operation_id；无 current 时成为 current，否则按位置入队 |
| task_list | project_id；可选 status: open（默认）/current/queued/pending_review/archived/history/completed/cancelled/all，query:最多500字符，limit:1–100 默认20，offset 默认0；返回 items、total、has_more、next_offset、counts |
| task_read | task_id；可选 checkpoint_offset/checkpoint_limit、phase_offset/phase_limit、work_unit_limit；默认返回最近10个检查点、最近20个阶段和最多30个 WorkUnit，并返回各自 total/truncated/next_offset |
| task_checkpoint | task_id, expected_revision, expected_etag, progress, next_step, operation_id；可选 findings、failures、verification、attest_contract:false；写入独立 checkpoint Markdown，不追加任务正文 |
| task_close | task_id, expected_revision, expected_etag, outcome: completed/cancelled, summary, acceptance_results: 键为任务原 acceptance 文本、值为逐项说明的对象, operation_id；可选 abandon_work_units:false；关闭 current 后原子推进下一 queued |
| work_unit_update | 创建：task_id, title, owner, status(pending/running), operation_id；更新：unit_id, expected_revision, expected_etag, status, operation_id；可带 findings/progress/failures/blocker/verification/next_step |
| task_phase_close | task_id, expected_revision, expected_etag, title, summary, next_step, operation_id；可选 findings/verification/abandon_work_units，阶段结束但任务保持 current |
| memory_write | title, body, sources: `{type,reference}` 来源对象数组, operation_id；scope 默认 project，需要 project_id；task 范围另需 task_id；user 范围无需 project_id；kind 默认 fact；status: CLI/Service 默认 active，MCP 代理入口默认 candidate；两者都可 candidate |
| memory_read | memory_id |
| memory_search | scope 默认 project；project_id、task_id 随范围要求；query 默认空；limit 1–100，默认 20；status 默认 active，也可 candidate |
| memory_update | memory_id, expected_revision, expected_etag, title, body, operation_id |
| memory_forget | memory_id, expected_revision, expected_etag, operation_id |
| memory_activate | memory_id, expected_revision, expected_etag, operation_id；候选审核后激活 |
| memory_review | task_id；可选 phase_id；返回任务或阶段证据与已有知识 |
| memory_review_complete | task_id, expected_revision, expected_etag, memory_ids:同项目知识ID数组, summary, operation_id；可选 phase_id，阶段审查时 revision/etag 属于阶段 |
| context_get | project_id；可选 task_id, query, include_user:false, budget:500–32000 字符 |
| project_scan | project_id, operation_id |
| graph_query | project_id；可选 query, limit:1–1000, include_stale:false；架构节点含 revision、etag、relations、stale_reasons |
| evidence_read | project_id, path:已扫描相对路径；可选 max_chars:1–100000 |
| graph_write | project_id, title, body, sources: `{path,hash}` 来源对象数组, relations: `{source,target,relation,evidence_kind,certainty}` 关系对象数组, operation_id |
| graph_update | project_id, architecture_id, expected_revision, expected_etag, title, body, sources, relations, operation_id；来源哈希必须匹配最近扫描和当前内容 |
| graph_forget | project_id, architecture_id, expected_revision, expected_etag, operation_id；删除 Markdown、更新索引并写入 tombstone |
| status | 可选 project_id（CLI/Web）；返回全局记录计数、知识 scope/status 聚合、架构 stale 汇总和索引状态；传入 project_id 时另返回同口径 project 子对象 |
| recovery_status | 无 |
| recover | 无 |
| recovery_resolve | operation_id:待处理操作, expected_files:{相对路径:当前etag或null} |
| rebuild | 无，CLI 运维入口 |

`work_unit_update` 的独立 revision/etag 只串行化记录写入，避免子代理覆盖彼此进度；它不隔离代码工作树、测试产物或 Git 状态。并行写入同一项目时，宿主必须分配独立 worktree 或互不重叠的文件范围，并在父任务中合并验证。

MCP 核心写工具使用扁平字符串数组，避免 Cursor 等桌面客户端因嵌套对象 schema 丢失全部参数；CLI `call` 和 `Service` action 仍使用上表的结构化对象。`memweave_task_close.acceptance_results` 按原验收顺序传结果字符串；`memweave_memory_write.sources` 每项为 `type:reference`，type 可为 user/file/task/host；`memweave_graph_write`、`graph_update` 的 `sources` 每项为 `path|sha256`，`relations` 每项为 `source|target|relation`，可选追加 `|evidence_kind|certainty`。服务端会还原并继续执行原有来源校验。来源不是服务独立验证过的真相。知识种类支持 fact、decision、preference、lesson、constraint、procedure。

`graph_query` 的架构节点包含 `revision`、`etag`、`sources`、`relations`、`stale_reasons` 和当前 `status`；`include_stale=true` 才能查看过期记录。调用 `graph_update` 或 `graph_forget` 前必须读取这些字段，不能从标题推测版本。更新会重新核对来源是否仍在最近扫描中且文件哈希未变化；遗忘使用现有 tombstone，同一记录 ID 不能被重建。

`system_status.knowledge` 返回知识 `total`、按 project/task/user 的 `by_scope`、active/candidate 的 `by_status` 和交叉计数 `by_scope_status`。`system_status.architecture` 返回架构 `total`、`active`、当前 `stale` 数量与 ID，以及无法核验的项目错误或记录。统计口径直接遍历同一 Markdown 记录和扫描证据，不维护第二套数据库。

`status(project_id=...)` 复用同一聚合逻辑，并在 `project` 下返回该项目的 `tasks`、`knowledge` 与 `architecture`；用户范围知识不混入项目事实。MCP `memweave_system_status` 仍只提供全局只读概览，项目视图由 CLI/Web 的 `status` action 提供，不新增第二套统计工具。

MCP 的 `memweave_memory_write` 默认 `status=candidate`，面向 Agent 的写入不会自动成为 active；`memweave_memory_activate` 才是显式提升。CLI/Web 的用户主动录入仍可显式使用 active。project scope 只返回同一项目，task scope 只返回同一任务，user scope 必须包含 `user` 来源、不能绑定 task，并且必须显式 `include_user=true`；不要把项目事实写入 user scope 来绕过跨项目隔离。`memory_review` 和 `memory_review_complete` 不自动生成或激活知识。

任务完成与知识提炼分别记录。完成 current 任务后自动把最早 queued 任务设为 current，并保留前一任务的待提炼状态。任务审查完成时记录 archived_at；旧的 completed+reviewed 记录也按归档处理。阶段收束写入独立 phase 记录，也可触发知识审查，但任务仍为 current。没有活跃宿主时保留待办，不自动调用模型。宿主用 memory_review 准备证据、检索去重，再写候选、激活或修订已有知识；memory_review_complete 记录提炼结果，允许空 memory_ids 并说明没有可复用内容。候选不进入默认检索或上下文；网页可单独查看和激活。用户明确录入可直接保存为 active。

架构关系支持 `self` 作为当前记录端点，文件端点为 `file:src/app.py`。`validated_by` 关系需要明确 evidence_kind：test_declaration、static_association、host_reported 或 actual_coverage；该标签仍由宿主提交，不等于服务器运行过测试。来源必须匹配扫描哈希及当前内容。

`memory_search` 通过 Basic Memory 的 SQLite FTS5 索引检索 Markdown，并按 `scope`、`project_id`、`task_id`、`status`、`note_type` 元数据过滤候选；返回结果的排序和分词由固定版本的上游实现决定，不再维护 MemWeave 自己的词法评分。每个结果包含 `retrieval.permalink`、`retrieval.score` 以及当前 Markdown 的 revision/etag；`memory_read` 保留在 CLI/管理兼容入口，不属于 Agent 的通用工具面。标题、正文与 `Retrieval cues:` 都可参与召回；当前没有语义/向量检索，不理解任意同义或跨语言关系，也不调用模型。搜索覆盖声明为 `basic_memory_sqlite_fts5`。

## Agent 工作入口与上下文

`workspace_prepare` 是只读准备入口，不创建项目、任务或检查点。`root` 必须是绝对执行根；子目录不自动归并，不同 clone 仍分别登记。同一 Git common directory 下的 linked worktree 会解析到主 checkout 已登记的项目，共用同一 `project_id` 和任务队列，只改变执行根，不产生第二个项目或 current。返回：

- `state`：registration_required、ready 或 no_active_task。顶层任务不再有“多 active 歧义”。
- `project`：匹配的 id/title/root、execution_root 与 is_worktree；未注册为 null。
- `execution_root`、`registered_root`、`is_worktree`：当前绝对执行根、对应的已登记项目根，以及两者是否指向同一目录；未注册时 registered_root 为 null。
- `tasks`：最多 20 项 current/queued 简表，current 在前；`pending_consolidation`：已完成但待提炼的任务；`pending_phases`：待提炼阶段；`work_units`：打开任务下的 pending/running/blocked WorkUnit 简表；`history`：最多 5 项近期已归档任务摘要；`task_counts`：current/queued/pending_review/completed/cancelled/archived/total。各有截断标识。
- `task_id` 与 `context`：inspect 不选择任务或生成上下文；new 生成项目上下文但不继承旧任务；resume 只选择显式 current task_id 或唯一 current。没有 current 时 context 为 null；queued 任务不能被 resume。`attention` 和 `completion` 顶层字段与 `context` 可读取时保持一致；inspect/no_active_task 也会返回能从任务队列计算的待提炼或阻塞提示。

`context_get` 的 `text` 按总字符预算组合 current 任务摘要、运行中/阻塞 WorkUnit、知识和局部图谱；最新下一步在任务摘要前部，历史使用剩余空间。`reboot` 是结构化五问摘要：`where`、`goal`、`done`、`failed`、`next`，并带 `contract`、`attention`、`completion` 和可读 `text`。`contract.state` 为 `attested`、`drift` 或 `unattested`；它只保护 goal+acceptance 的摘要，不是第二套计划文件。`attention` 的稳定 code 包括 `task_not_current`、`blocked_work_units`、`next_step_missing`、`task_contract_drift`、`task_contract_unattested`、`graph_stale`、`graph_unknown` 和 `pending_consolidation`；`completion.state` 为 `ready`、`blocked` 或 `not_applicable`，只表示是否还有打开的 WorkUnit，不表示验收已经通过。WorkUnit 包含负责人、进展、失败、阻塞、验证与下一步。知识与图谱各有预算份额，小预算优先返回少量可读片段。图谱当前使用至多 40 个子串匹配节点，并优先呈现有效架构记录；多词语义匹配仍需宿主分别查图和原始证据。无 query 且存在 current task 时，只用任务标题/目标构造一次知识查询。

`context_get` 的候选知识来自 Basic Memory FTS；没有匹配时不注入近期知识兜底。显式 query 或当前任务标题/目标会作为一次检索输入；task 范围在当前任务无 query 时可直接纳入，项目范围和用户范围不使用背景兜底。命中的知识会附带 Basic Memory context 的 primary/related/observation 覆盖，正文仍读取 Markdown 权威记录，避免把索引副本当业务真相。

`candidate_sources` 是候选标识；`included_sources:[{id,type,truncated,reason?}]` 是实际写入 text 的来源和片段截断状态。记忆来源的 `reason` 为 `matched`、`task_query`、`task_scope` 或 `preference`；没有 reason 的来源不是记忆。`memory` 报告 `strategy`、`query_source`、Context 检查次数、关系和 observation 覆盖以及错误；`recent_fallback` 固定为 false。text 的 `truncated` 不代替底层搜索/图谱的覆盖声明。`graph.coverage/freshness` 报告扫描边界及来源变化，stale_files 最多列 20 项，另有 stale_file_count。来源变化的架构和文件不作为有效正文加入。新文件需要重新扫描；返回的图谱 metadata 不含所有节点正文。

新建任务的 goal+acceptance 会写入 `contract_hash`；外部编辑导致 `task_contract_drift` 时，先向用户确认，再用最新 revision/etag 调用 `task_checkpoint(attest_contract=true)` 重新确认。`task_contract_unattested` 只表示旧任务没有摘要，不是已确认。

user 范围仍需 include_user:true；显式选择后，长期 preference 类型不要求与代码查询词相同，其余用户知识仍按 query 检索。所有记忆仍服从当前指令，不保证仅凭 FTS 匹配即可判断适用性。context 的字符上限约束 text，JSON 元数据另行返回。

## 本地文件

记忆库默认位于 `%USERPROFILE%\.memweave`，含 `config.toml`、`projects/`、`tasks/`、`work_units/`、`phases/`、`checkpoints/`、`knowledge/`、`architecture/`、`index/memory.sqlite3`、`index/basic-memory/memory.db` 与 `runtime/`。任务保留目标、验收和当前摘要，正文不再随每次检查点或阶段收束增长；检查点和阶段各有独立 Markdown。WorkUnit 各自保存独立 revision/etag；长期知识修订替换当前正文。frontmatter 是 JSON 映射形式的有效 YAML 子集，手动编辑时需保留字段和 JSON 格式。支持 UTF-8 与 CRLF，单文档最多 256 KiB。

Markdown 是业务记录权威。`index/basic-memory/memory.db` 是可删除并从 Markdown 重建的搜索/上下文索引；`index/memory.sqlite3` 保存操作幂等日志、遗忘墓碑、探针、扫描和 Hook 会话等运行状态，**不可当作普通缓存清空**。备份整个记忆库，服务停止后复制最简单；在线备份需正确处理 SQLite WAL。不要只复制某一个数据库主文件。

受控写入使用跨进程锁及准备日志，再原子替换文件；读和列表操作获取同一锁并先恢复可完成的 pending 写入，再读取 Markdown，避免正常并发写入被误报为中断。记录级 revision 与 etag 同时匹配才可更新，手动修改正文也会改变 etag。外部编辑器不遵守 MemWeave 的锁，使用时避免同时保存同一文件；检测和替换之间极短的系统调用窗口不能提供跨编辑器的绝对原子比较保证。断电后的磁盘级持久性尚未做硬件故障验证。

`rebuild` 在原 SQLite 文件内清空 Basic Memory 派生表并从当前 Markdown 重建，保留项目行、schema、操作历史、墓碑和其他运行状态；其他 MCP 进程仍可持有同一数据库连接，不再依赖停服删除数据库文件。重建失败的记录会进入 `search_index.failed`，Markdown 不会被改写。扫描文件索引丢失后重新扫描。遗忘清除当前文档和派生索引条目，并防止旧请求复活相同 ID；不承诺取证级安全擦除，也不会改写外部备份或已导出的内容。

## 中断与冲突恢复

正常中断运行 `memweave recover --home <库>`，或重新启动服务即可恢复。若文件同时被外部修改，返回 `RECOVERY_REQUIRED`，保留当前内容并暂时阻止普通记录访问。

1. 停止编辑并备份整个库。用 `call recovery_status --input '{}'` 查看操作、路径、当前/预期指纹与 proposed_record。
2. 若决定完成原操作，先把当前外部版本另存到库外，再将目标文件修复到原版本或 proposed_record 对应的 Markdown，然后运行 `recover`。不要直接改数据库。
3. 若决定保留当前文件，确保它是有效 Markdown，保留原 ID；把状态中的每个路径与 current_etag 放入 expected_files，调用 `recovery_resolve`。它放弃该笔未完成写入、保留当前有效内容并重新索引。文件已删除时值为 null；删除会留下墓碑。
4. 文件若再次变化，解决操作会报版本冲突，需重新检查。成功返回 abandoned:true；旧请求重试也返回放弃结果，不会重新覆盖文件。

## HTTP

`ui` 只监听 127.0.0.1，启动输出带令牌的 URL。页面将令牌移到 sessionStorage 并清理 URL。API 使用 Bearer 头，校验 Host、Origin 与跨站标识；不提供跨域访问。GET `/api/status` 返回全局状态；POST `/api/status` 可带 `project_id` 返回项目视图，其余 POST `/api/{action}` 执行服务 action。JSON 对象最大 256 KiB；请求内容与令牌不写访问日志。会话令牌仅保护本地 HTTP API，不替代操作系统账户隔离。

管理页的项目图谱使用 `graph_query(include_stale=true)` 读取节点。纠正表单保留查询返回的 `sources` 与 `relations`，把当前 revision/etag 作为 expected 值；来源漂移时必须由用户确认后重新 `project_scan`，再逐项 `evidence_read` 取得当前哈希并提交 `graph_update`。遗忘表单要求输入确认词，提交 `graph_forget` 后刷新同一状态源，不另行维护页面计数。
