# API 与存储

> 状态：本文描述 **当前开发接口**。主 MCP 由 Basic Memory 单一服务生命周期持有 Markdown 存储、SQLite 派生索引和 FTS 检索；主任务使用单 `current` 顺序队列，任务内 WorkUnit 可并行，阶段可独立收束；显式侧任务不进入主队列。22 工具服务契约对所有 stdio MCP 客户端相同；完整激活还要求 Skill/Rule 和启动通道，Hook 只是启动通道的一种实现。只连接 MCP、没有 always-on Rule/Hook/宿主指令时，不能宣称完整体验。普通检索和维护使用常驻 MCP。Codex 的低频 Hook 包含压缩快照与按需恢复；Cursor/Devin 没有 MemWeave 压缩 Hook，靠首轮回退和主动检查点恢复。产品与运行时设计见 [架构说明](architecture.md)。

当前 MCP 只有一个 22 个工具的通用面；下表描述的全部业务 action 仍可通过 CLI 使用。诊断探针、项目列表、完整上下文读取、架构遗忘和恢复入口不暴露给 Agent，分别走 CLI、内部探针或管理页。`workspace_prepare` 获取默认工作集，`memory_search` 返回当前知识版本与 etag；`memory_read`、`context_get`、`project_list`、`graph_forget` 和恢复动作仍保留在 CLI/管理路径。不提供 `core/full` 或轻量/全量选择，旧 `--toolset` 参数只为迁移兼容并被忽略。

会话开始时 Hook 复用 `workspace_prepare`，只恢复唯一 `current` 任务、运行中/阻塞 WorkUnit、待审查项和近期归档概况；`resume` 还会取得临时执行租约，queued 任务不会抢占。Codex 额外以 `PreCompact` 保存有界工作快照、以 `UserPromptSubmit` 消费 pending 快照，二者不启动搜索索引。快照可包含最近用户/助手文本摘录与工具名称，不包含命令、工具参数或输出；`Last user` 会剔除 clipboard/环境/AGENTS/内部 goal 等脚手架，只保留可识别的真实请求。收据仍只保留元数据。`PreCompact` 写 current 任务检查点走内部 `Service.task_compaction_snapshot`：跳过执行租约但保留 revision/etag CAS 与操作幂等，不获取、释放或转移任务执行所有权；写入失败会记录在 Hook 会话状态，并在下一次压缩恢复块中显式提示“未收到压缩前检查点”，不伪造成功。默认不注册 `Stop`、`PostToolUse`、`afterMCPExecution` 或 `afterFileEdit`。普通检索和任务维护由 Agent 在常驻 MCP 中完成。Cursor 的 `additional_context` 不等于模型已收到；常驻 MCP 可用时，首个正常请求必须先恢复任务，不能等待启动块或要求用户重述。Agent 面 MCP 进程会硬拒绝：在本进程对某项目成功 `workspace_prepare`（`ready`/`no_active_task`/`task_claimed`）之前，其他项目工具返回 `PREPARE_REQUIRED`；`status`/`project_register`/`workspace_prepare` 不受限。CLI 与管理页默认不启用该硬门。

默认恢复文本包含最新检查点的有界 `findings`，不追加重复任务正文；无显式查询时不列出无关文件节点。知识检索使用 `basic_memory_sqlite_fts5`，不为仅返回计数而额外展开关系上下文。`workspace_prepare.history` 最多包含 5 条归档的 `id/title/status/revision/archived_at/lifecycle`；完整摘要仍通过 `task_list` 或 `task_read` 显式读取。

`task_checkpoint` 省略或传 `null` 的 `findings`、`failures`、`verification`、`evidence` 会保留当前证据；明确传空串/空数组才清除该字段。每个独立检查点保存解析后的完整当前值，幂等请求仍按调用者实际提交的字段判断。接续正文逐项显示原始验收及是否已有宿主报告；`completion.acceptance_reported_count` 和 `acceptance_unreported_count` 仅统计明确的 `acceptance_results`，不从进展或测试数量推断通过，`ready` 仍只表示无打开 WorkUnit。

Codex 捕获最近用户文本时排除独立的内部 goal、环境、AGENTS 与 Hook 消息。消费 pending 时只读核对同项目 current，并重新装配最新任务字段；语义状态改变时舍弃旧对话摘录，读取失败只返回重新连接提示，旧 pending 格式不直接重放。此路径不启动搜索索引，快照仍只包含有界摘录和工具名称。它不能自动识别任意自然语言结论矛盾。

CLI、MCP 和网页共享 `Service`。CLI `call` 使用下表 action；MCP 使用 `memweave_` 前缀，状态工具名是 `memweave_system_status`。标准成功返回 `{status:"ok",data:...}`，失败为领域错误。MCP 使用协议工具错误，CLI 错误写 stderr 且退出码为 1。

```powershell
$request = @{root='C:\Projects\example'; name='example'; operation_id='register-unique-id'} | ConvertTo-Json
$request | & '.\.venv\Scripts\python.exe' -m memweave call project_register --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave call project_list --input '{}' --home "$HOME\.memweave"
```

CLI 支持 `--input <JSON>` 或 stdin UTF-8 JSON，最大 256 KiB。为避免复杂 JSON 转义，推荐通过 stdin 传对象。Windows PowerShell 5.1 的管道可能将中文转换为问号；批处理中文 JSON 请用 PowerShell 7，或从 Python 的 UTF-8 管道调用，网页和 MCP 无此限制。

`memweave clients doctor [--home HOME] [--root DIR ...] [--client all|cursor|codex|devin] [--fix] [--json]` 是只读 CLI 运维入口，不作为 MCP action。默认根目录集合由活动库已登记项目和 Codex 全局 `projects` 中存在的目录组成；显式 `--root` 追加目录。返回顶层 `state`（`ready|needs_attention|repaired`）、`roots_scanned`、`targets_scanned`、状态计数和逐目标 `targets[]`。目标区分 `global|project` 与 `source=registered|codex_known|explicit`，问题 code 包括 `stale_home`、`version_mismatch`、`server_mismatch`、`interpreter_missing`、`mcp_mismatch`、`hooks_missing`、`skill_drift`、`rule_drift`、`manifest_drift`、`project_unregistered`、`duplicate_mcp`、`config_invalid`、`unmanaged_installation`、`global_config_invalid`、`mcp_global_mismatch` 和 `root_invalid`。Devin 的全局 MCP 与项目 Skill/Rule 分开报告，缺失全局配置不会被 `--fix` 静默写入。

`--fix` 仅对项目级可修复项调用现有 `setup`，结果包含 `repair.state`、修改文件、备份和 `restart_required`；健康目标不写文件。单个目标修复遇到 `OSError`/`DiagnosticError` 时按 target 记录 `repair.state=failed`、`repair.error_code=FIX_FAILED` 与 `repair.error_type`，并添加 `fix_failed` issue 后继续处理其他目标，不再整体中止；全局入口没有自动修复路径，`duplicate_mcp` 和受管资源冲突默认要求人工合并。命令的成功/失败退出码分别为 0/1，修复成功但需要客户端重载仍返回 0；`clients doctor` 不启动或重载客户端，不能替代 Hook 注入与宿主使用验收。

`memweave config --client cursor|codex|devin|generic --home HOME` 分别输出 Cursor/Devin/generic 的 `mcpServers` JSON 或 Codex TOML；只有 `cursor` 和 `codex` 支持 `--kind hooks`。`memweave setup/check --client devin` 的 MCP 配置是用户级 `mcp_config.json`，项目级资源是 `.devin/skills` 和 `.devin/rules`；不增加 MCP 工具或 Hook 事件。测试可通过隐藏的 `--global-base` 隔离用户级路径。

| action | 必填参数与主要可选参数 |
|---|---|
| project_register | root:绝对文件夹、name、operation_id；同一文件夹跨客户端复用同一 project；显式子文件夹独立，未登记子目录解析到最近已登记父项目 |
| project_list | 无；返回每个项目推导的 parent_id（最近已登记祖先，排除自己） |
| workspace_prepare | root:存在的绝对项目根目录（MCP 兼容别名 execution_root/cwd/project_root）；intent:inspect/new/resume，默认 inspect（兼容 mode；存在 task_id 时推断 resume）；可选 task_id、query、include_user:false、include_group_knowledge:false、budget:500–32000（低于 500 时 clamp）、execution_claim（仅独立进程的委派子代理）、include_knowledge_index:false（实验性 active-only 索引头注入） |
| task_open | project_id, title, goal, acceptance: 字符串数组, operation_id；可选 parent_task_id：显式侧任务；无 current 时普通任务成为 current，否则按位置入队；新任务 ID 为本地分钟 task_YYYYMMDD-HHMM，同分钟冲突追加 -02 |
| task_list | project_id；可选 status: open（默认）/current/queued/side/pending_review/archived/history/completed/cancelled/all，query:最多500字符，limit:1–100 默认20，offset 默认0；返回 items、total、has_more、next_offset、counts |
| task_read | task_id；可选 checkpoint_offset/checkpoint_limit、phase_offset/phase_limit、work_unit_limit；默认返回最近10个检查点、最近20个阶段和最多30个 WorkUnit，并返回各自 total/truncated/next_offset |
| task_checkpoint | task_id, expected_revision（兼容 revision）, expected_etag（兼容 etag）, progress, next_step, operation_id；可选 findings、failures、verification、evidence、attest_contract:false；写入独立 checkpoint Markdown，不追加任务正文；省略 evidence 保留旧值，显式空数组清除 |
| task_close | task_id, expected_revision（兼容 revision）, expected_etag（兼容 etag）, outcome: completed/cancelled（兼容 success/done/complete/cancel/canceled）, summary, operation_id；验收可按顺序传 acceptance_results 字符串数组或单个字符串，也可传 acceptance_results_text（每行一条或 `---` 分隔）；可选 abandon_work_units:false；空/部分参数返回明确“任务未关闭”恢复提示；关闭 current 后原子推进下一 queued；关闭侧任务不推进主队列 |
| work_unit_update | 创建：task_id, title, owner, status(pending/running), operation_id；更新：unit_id, expected_revision, expected_etag, status, operation_id；可带 findings/progress/failures/blocker/verification/next_step；更新时重复传未变化的 title/owner 被忽略，真正修改仍返回不可变错误；已有单元可由委派子代理按 unit ID 更新，创建仍需父任务 claim，不按 WorkUnit 单独申请主任务租约 |
| task_phase_close | task_id, expected_revision, expected_etag, title, summary, next_step, operation_id；可选 findings/verification/abandon_work_units，阶段结束但任务保持 current |
| memory_write | title, body, sources: `{type,reference}` 来源对象数组, operation_id；可选 evidence（≤20 条结构化证据指针）；scope 默认 project，需要 project_id；group 范围写在父项目，要求至少一个直接子项目；user 范围无需 project_id，且必须包含显式 user 来源；kind 默认 fact；status: CLI/Service 默认 active，MCP 代理入口默认 candidate；两者都可 candidate |
| memory_read | memory_id；返回记录正文、sources 与 evidence |
| memory_search | scope 默认 project；project_id 随范围要求；query 默认空；limit 1–100，默认 20；status 默认 active，也可 candidate 或 retired；可选 include_group:false、include_members:false（仅 project scope）；结果含 evidence |
| memory_update | memory_id, expected_revision, expected_etag, title, body, operation_id；可选 status:candidate/retired、evidence（省略保留、空数组清除），省略 status 保持原状态；激活只走 memory_activate |
| memory_forget | memory_id, expected_revision, expected_etag, operation_id |
| memory_activate | memory_id, expected_revision, expected_etag, operation_id；候选审核后激活 |
| memory_review | task_id；可选 phase_id；返回任务或阶段证据、已有知识与有界 `consolidation` 巩固包（knowledge_index、最近检查点+evidence、active/candidate 知识、待审阶段、lint 与截断说明）；不自动写知识、不调用模型 |
| memory_review_complete | task_id, expected_revision, expected_etag, memory_ids:同项目知识ID数组, summary, operation_id；任务级只接受已 completed 任务，关闭并释放 claim 后仍可审查，无 live claim 时不因残留锁文件阻断；可选 phase_id，阶段审查时 revision/etag 属于阶段且仍要求有效执行权 |
| context_get | project_id；可选 task_id, query, include_user:false, include_group_knowledge:false, include_knowledge_index:false, budget:500–32000 字符 |
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
| knowledge_index | project_id；只读生成项目知识索引（active/candidate 一行一条，带硬上限、total/returned/truncated）；CLI/Service 可用，不暴露为 MCP 工具，不写文件、不新增索引数据库 |

`work_unit_update` 的独立 revision/etag 只串行化记录写入，避免子代理覆盖彼此进度；它不隔离代码工作树、测试产物或 Git 状态。并行写入同一项目时，宿主必须分配独立 worktree 或互不重叠的文件范围，并在父任务中合并验证。

MCP 核心写工具使用扁平字符串数组，避免 Cursor 等桌面客户端因嵌套对象 schema 丢失全部参数；CLI `call` 和 `Service` action 仍使用上表的结构化对象。`memweave_task_close` 优先使用 `acceptance_results` 按原验收顺序传结果字符串，也接受一个按换行/`---` 分隔的字符串；桌面客户端丢失长数组或 JSON 时可改传 `acceptance_results_text`，每行一条或 `---` 分隔。缺失参数在任何写入前返回“任务未关闭”并提示 `task_read` 核对与重载/新会话恢复。`memweave_memory_write.sources` 首选 `type:reference`，type 可为 user/file/task/host；普通文件路径、task/checkpoint ID、commit SHA 和 URL 会被安全推断，无法识别时仍拒绝。`evidence` 每项为 `kind|reference[|hash[|observed_at[|summary]]]`，kind 可为 user/file/command/test/url/host，服务端转成结构化指针并校验上限（20 条；reference≤500、hash≤128、observed_at≤64、summary≤500；序列化≤20000）；`memweave_graph_write`、`graph_update` 的 `sources` 每项为 `path|sha256`，`relations` 每项为 `source|target|relation`，可选追加 `|evidence_kind|certainty`。服务端会还原并继续执行原有来源校验。来源不是服务独立验证过的真相。知识种类支持 fact、decision、preference、lesson、constraint、procedure。

`graph_query` 的架构节点包含 `revision`、`etag`、`sources`、`relations`、`stale_reasons` 和当前 `status`；`include_stale=true` 才能查看过期记录。调用 `graph_update` 或 `graph_forget` 前必须读取这些字段，不能从标题推测版本。更新会重新核对来源是否仍在最近扫描中且文件哈希未变化；遗忘使用现有 tombstone，同一记录 ID 不能被重建。

`system_status.knowledge` 返回知识 `total`、按 project/group/user 的 `by_scope`、active/candidate/retired 的 `by_status` 和交叉计数 `by_scope_status`。`system_status.architecture` 返回架构 `total`、`active`、当前 `stale` 数量与 ID，以及无法核验的项目错误或记录。统计口径直接遍历同一 Markdown 记录和扫描证据，不维护第二套数据库。

`status(project_id=...)` 复用同一聚合逻辑，并在 `project` 下返回该项目的 `tasks`、`knowledge` 与 `architecture`；用户范围知识不混入项目事实。MCP `memweave_system_status` 仍只提供全局只读概览，项目视图由 CLI/Web 的 `status` action 提供，不新增第二套统计工具。

MCP 的 `memweave_memory_write` 默认 `status=candidate`，面向 Agent 的写入不会自动成为 active；`memweave_memory_activate` 才是显式提升。CLI/Web 的用户主动录入仍可显式使用 active。project scope 只返回同一项目，user scope 必须包含 `user` 来源，并且必须显式 `include_user=true`；不要把项目事实写入 user scope 来绕过跨项目隔离。`memory_review` 和 `memory_review_complete` 不自动生成或激活知识。

用户取消或新证据推翻一条知识时，宿主应修正同一记录；不再适用的内容可通过 `memory_update(status="retired")` 留下取代原因并退出默认召回。Markdown、来源、ID 和修订仍保留，`memory_search(status="retired")` 可显式查询，索引重建不会重新激活。退役知识要重新使用，必须先更新为 candidate，再显式审核激活；`memory_update` 不能直接设置 active。省略 status 的旧调用保持原状态。该操作不会自动发现或关闭其他矛盾记录，也不会清除已发出的上下文。

任务完成与知识提炼分别记录。完成 current 任务后自动把最早 queued 任务设为 current，并保留前一任务的待提炼状态。任务审查完成时记录 archived_at；旧的 completed+reviewed 记录也按归档处理。任务级审查只允许 completed 任务，执行租约在 task_close 时已经释放，因此没有 live claim JSON 时不会因残留 `.lock` 阻断；若仍有真实 claim，则仍须由其持有者审查。阶段收束写入独立 phase 记录，也可触发知识审查，但任务仍为 current 且阶段审查继续受执行权约束。没有活跃宿主时保留待办，不自动调用模型。宿主用 memory_review 准备证据、检索去重，再写候选、激活或修订已有知识；memory_review_complete 记录提炼结果，允许空 memory_ids 并说明没有可复用内容。候选不进入默认检索或上下文；网页可单独查看和激活。用户明确录入可直接保存为 active。

`memory_review` 的 `consolidation` 是只读、非模型的巩固包：`knowledge_index` 给出 active 知识的一行一条导航（以及 candidate 区）、`recent_checkpoints` 最近 10 个检查点（findings 摘要 + evidence）、`existing_knowledge` 的 active/candidate 各最多 50 条摘要、`pending_phases` 与 `pending_consolidation` 各最多 20 条，以及 lint（规范化重复标题、active/candidate 无 evidence、candidate 积压与最旧时间、retired 计数）。每个集合带 `total/returned/truncated`，整体带 `limits` 与 `truncated_reasons`。它只准备证据，不写知识、不激活、不调用模型；宿主按 continuity Skill 的低频巩固作业处理。

`knowledge_index` 是独立只读查看入口（CLI `memweave call knowledge_index --input '{"project_id":"..."}'` 或 Service action），从现有 Markdown 知识记录即时生成，不写入文件、不新增数据库、不进入 MCP 工具面。索引有 active/candidate 行数、单行长度和总字符上限，超限返回 `truncated` 与覆盖说明。默认**不注入** `workspace_prepare`/`context_get`；可用 `include_knowledge_index:true` 做实验性 active-only 注入（默认 false），是否默认开启由评估决定；索引不能当第二套事实源。

架构关系支持 `self` 作为当前记录端点，文件端点为 `file:src/app.py`。`validated_by` 关系需要明确 evidence_kind：test_declaration、static_association、host_reported 或 actual_coverage；该标签仍由宿主提交，不等于服务器运行过测试。来源必须匹配扫描哈希及当前内容。

`memory_search` 通过 Basic Memory 的 SQLite FTS5 索引检索 Markdown，并按 `scope`、`project_id`、`status`、`note_type` 元数据过滤候选；返回结果的排序和分词由固定版本的上游实现决定，不再维护 MemWeave 自己的词法评分。每个结果包含 `retrieval.permalink`、`retrieval.score` 以及当前 Markdown 的 revision/etag；`memory_read` 保留在 CLI/管理兼容入口，不属于 Agent 的通用工具面。标题、正文与 `Retrieval cues:` 都可参与召回；当前没有语义/向量检索，不理解任意同义或跨语言关系，也不调用模型。搜索覆盖声明为 `basic_memory_sqlite_fts5`。

`scope=group` 的知识写在父项目，只有存在直接子项目时允许写入；子项目用 `memory_search(include_group=true)` 显式检索最近父项目的组知识。`include_members=true` 只对父项目生效，检索直接成员项目的 project 知识正文。两者默认 false，不会进入 `workspace_prepare`/`context_get` 的默认上下文；成员知识对父项目只提供摘要（`knowledge_counts`/`recent_knowledge`），正文按需检索。

## Agent 工作入口与上下文

`workspace_prepare` 不创建项目、任务或检查点；`resume` 会在 `runtime/execution/` 写入临时执行租约，不修改项目 Markdown。纯状态查询应使用 `inspect` + `task_read`，保持不写租约。租约按 MCP 服务实例存活，不是认证令牌；同一进程承载多个对话时无法区分对话，不能据此宣称会话级隔离。`root` 必须是绝对执行根。项目身份是绝对文件夹：精确文件夹优先；显式登记的子文件夹是独立项目，未登记子目录解析到最近已登记父项目；linked worktree 解析到主 checkout 项目，共用同一 `project_id` 和任务队列，只改变执行根，不产生第二个项目或 current。返回：

- `state`：registration_required、ready、no_active_task 或 task_claimed。顶层任务不再有“多 active 歧义”；task_claimed 表示另一个活 MCP 实例拥有该任务的执行租约。
- `project`：匹配的 id/title/root、execution_root、is_worktree 与 is_subfolder；未注册为 null。
- `execution_root`、`registered_root`、`is_worktree`、`is_subfolder`：当前绝对执行根、对应的已登记项目根、是否为 linked worktree 执行别名，以及是否为已登记项目的子目录；未注册时 registered_root 为 null。
- `tasks`：最多 20 项 current/queued/side 简表，current 在前；new 返回空数组；`background_task`：当前主任务 id/title/claim 状态，不能当作新会话任务；`pending_consolidation`：已完成但待提炼的任务；`pending_phases`：待提炼阶段；`work_units`：打开任务下的 pending/running/blocked WorkUnit 简表；`history`：最多 5 项近期已归档任务摘要；`task_counts`：current/queued/side/pending_review/completed/cancelled/archived/total。各有截断标识。
- `members`：只读的直接子项目摘要，最多 20 项；每项含 `id/title/root`、`current`（id/title/next_step）、`task_counts`、`knowledge_counts`、`recent_knowledge` 和 `attention`。成员关系由文件夹包含自动推导，不写项目记录；`members_truncated` 表示还有更多。`workspace_prepare(child)` 只返回 child 自己的任务和直接成员，不注入父项目任务或父项目成员。`include_group_knowledge:true` 才会把最近父项目/组的 `scope=group` 知识纳入 context；默认 false。
- `task_id` 与 `context`：inspect 不选择任务或生成上下文；new 生成项目上下文但不继承旧任务；resume 只选择显式 current/side task_id 或唯一 current。没有 current 时 context 为 null；queued 任务不能被 resume；另一个活 claim 返回 task_claimed 且不生成任务上下文。`attention` 和 `completion` 顶层字段与 `context` 可读取时保持一致；inspect/no_active_task 也会返回能从任务队列计算的待提炼或阻塞提示。
- `execution`：resume 成功时返回运行时 `claim_id`、`task_id` 与 `claimed_at`。它只用于当前驱动会话及其委派子代理；独立会话不得复用。

`context_get` 的 `text` 按总字符预算组合 current/side 任务摘要、运行中/阻塞 WorkUnit、知识和局部图谱；最新下一步在任务摘要前部，历史使用剩余空间。`include_knowledge_index:true` 时在预算内追加 active-only 知识索引头（≤30 行/≤4000 字符，带“导航说明”标题；candidate/retired 不出现；超限带截断说明），任务 brief 优先且默认 false 时输出与未启用版本一致。索引头只是导航，不是证据源，也不改变任务/知识记录。`reboot` 是结构化五问摘要：`where`、`goal`、`done`、`failed`、`next`，并带 `contract`、`attention`、`completion` 和可读 `text`。`contract.state` 为 `attested`、`drift` 或 `unattested`；它只保护 goal+acceptance 的摘要，不是第二套计划文件。`attention` 的稳定 code 包括 `task_claimed_elsewhere`、`task_not_current`、`blocked_work_units`、`next_step_missing`、`task_contract_drift`、`task_contract_unattested`、`graph_stale`、`graph_unknown` 和 `pending_consolidation`；`completion.state` 为 `ready`、`blocked` 或 `not_applicable`，只表示是否还有打开的 WorkUnit，不表示验收已经通过。WorkUnit 包含负责人、进展、失败、阻塞、验证与下一步。侧任务关闭不会自动恢复主任务。知识与图谱各有预算份额，小预算优先返回少量可读片段。图谱当前使用至多 40 个子串匹配节点，并优先呈现有效架构记录；多词语义匹配仍需宿主分别查图和原始证据。无 query 且存在 current task 时，只用任务标题/目标构造一次知识查询。

`context_get` 的候选知识来自 Basic Memory FTS；没有匹配时不注入近期知识兜底。显式 query 或当前任务标题/目标会作为一次检索输入；项目范围和用户范围不使用背景兜底。命中的知识会附带 Basic Memory context 的 primary/related/observation 覆盖，正文仍读取 Markdown 权威记录，避免把索引副本当业务真相。

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
