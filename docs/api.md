# API 与存储

0.2.3 的 MCP 默认 core 仅列出 20 个日常工具；下表描述的全部业务 action 仍可通过 CLI 使用。full 模式保留原 27 个 MCP 工具（包括诊断探针）。core 移出 probe_write/read、recovery_status/recover/recovery_resolve、project_list、context_get；使用 workspace_prepare 获取项目和上下文。恢复可用 CLI `recover`、`call recovery_status` 与 `call recovery_resolve`，原幂等及冲突协议不变。

CLI、MCP 和网页共享 `Service`。CLI `call` 使用下表 action；MCP 使用 `memweave_` 前缀，状态工具名是 `memweave_system_status`。标准成功返回 `{status:"ok",data:...}`，失败为领域错误。MCP 使用协议工具错误，CLI 错误写 stderr 且退出码为 1。

```powershell
$request = @{root='C:\Projects\example'; name='example'; operation_id='register-unique-id'} | ConvertTo-Json
$request | & '.\.venv\Scripts\python.exe' -m memweave call project_register --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave call project_list --input '{}' --home '.\work\runtime\memory home'
```

CLI 支持 `--input <JSON>` 或 stdin UTF-8 JSON，最大 256 KiB。为避免复杂 JSON 转义，推荐通过 stdin 传对象。Windows PowerShell 5.1 的管道可能将中文转换为问号；批处理中文 JSON 请用 PowerShell 7，或从 Python 的 UTF-8 管道调用，网页和 MCP 无此限制。

| action | 必填参数与主要可选参数 |
|---|---|
| project_register | root, name, operation_id |
| project_list | 无 |
| workspace_prepare | root:存在的绝对项目根目录；intent:inspect/new/resume，默认 inspect；可选 task_id（仅 resume）、query、include_user:false、budget:500–32000 |
| task_open | project_id, title, goal, acceptance: 字符串数组, operation_id |
| task_list | project_id；可选 status: active/completed/cancelled |
| task_read | task_id |
| task_checkpoint | task_id, expected_revision, expected_etag, findings, progress, next_step, operation_id |
| task_close | task_id, expected_revision, expected_etag, outcome: completed/cancelled, summary, acceptance_results: 条件→说明, operation_id |
| memory_write | title, body, sources, operation_id；scope 默认 project，需要 project_id；task 范围另需 task_id；user 范围无需 project_id；kind 默认 fact；status 默认 active，也可 candidate |
| memory_read | memory_id |
| memory_search | scope 默认 project；project_id、task_id 随范围要求；query 默认空；limit 1–100，默认 20；status 默认 active，也可 candidate |
| memory_update | memory_id, expected_revision, expected_etag, title, body, operation_id |
| memory_forget | memory_id, expected_revision, expected_etag, operation_id |
| memory_activate | memory_id, expected_revision, expected_etag, operation_id；候选审核后激活 |
| memory_review | task_id；返回待提炼状态、任务证据与已有知识 |
| memory_review_complete | task_id, expected_revision, expected_etag, memory_ids:同项目知识ID数组, summary, operation_id |
| context_get | project_id；可选 task_id, query, include_user:false, budget:500–32000 字符 |
| project_scan | project_id, operation_id |
| graph_query | project_id；可选 query, limit:1–1000, include_stale:false |
| evidence_read | project_id, path:已扫描相对路径；可选 max_chars:1–100000 |
| graph_write | project_id, title, body, sources:[{path,hash}], relations:[{source,target,relation}], operation_id |
| recovery_status | 无 |
| recover | 无 |
| recovery_resolve | operation_id:待处理操作, expected_files:{相对路径:当前etag或null} |
| rebuild | 无，CLI 运维入口 |

知识来源使用 `{type:"user"|"file"|"task"|"host",reference:"明确说明"}`，不是服务独立验证过的真相。知识种类支持 fact、decision、preference、lesson、constraint、procedure。

完成任务与知识提炼分别记录。完成后 consolidation_status 为 pending；没有活跃宿主时保留待办，不自动调用模型。宿主用 memory_review 准备证据、检索去重，再写候选、激活或修订已有知识；memory_review_complete 记录提炼结果，允许空 memory_ids 并说明没有可复用内容。候选不进入默认检索或上下文；网页可单独查看和激活。用户明确录入可直接保存为 active。

架构关系支持 `self` 作为当前记录端点，文件端点为 `file:src/app.py`。`validated_by` 关系需要明确 evidence_kind：test_declaration、static_association、host_reported 或 actual_coverage；该标签仍由宿主提交，不等于服务器运行过测试。来源必须匹配扫描哈希及当前内容。

列表及搜索先按范围限定候选，最多读取 1000 条记录；搜索结果按匹配分数和更新时间排序。中文包含单字与双字片段，英文按词匹配，不使用模型或向量。已知记录 ID 可直接读取或修改，不提供按 ID 的多用户授权隔离。

## Agent 工作入口与上下文

`workspace_prepare` 是只读准备入口，不创建项目、任务或检查点。root 精确匹配注册的绝对目录，子目录、克隆和 worktree 不自动归并。返回：

- `state`：registration_required、ready、task_selection_required 或 no_active_task。
- `project`：匹配的 id/title/root；未注册为 null。
- `tasks`：最多 20 项活跃任务简表；`pending_consolidation`：最多 20 项已完成但待提炼的简表；各有截断标识。底层任务列表最多读取 1000 项，达到上限时不会以“唯一任务”推断归属。
- `task_id` 与 `context`：inspect 均不选择任务或生成上下文；new 生成项目上下文但不继承旧任务；resume 仅在显式 task_id 或唯一活跃任务时生成任务上下文。有歧义或没有活跃任务时 context 为 null。

`context_get` 的 `text` 按总字符预算组合任务摘要、知识和局部图谱；最新下一步在任务摘要前部，历史使用剩余空间。知识与图谱各有预算份额，小预算优先返回少量可读片段。图谱当前使用至多 40 个子串匹配节点，并优先呈现有效架构记录；多词语义匹配仍需宿主分别查图和原始证据。

`candidate_sources` 是候选标识；`included_sources:[{id,type,truncated}]` 是实际写入 text 的来源和片段截断状态。text 的 `truncated` 不代替底层搜索/图谱的覆盖声明。`graph.coverage/freshness` 报告扫描边界及来源变化，stale_files 最多列 20 项，另有 stale_file_count。来源变化的架构和文件不作为有效正文加入。新文件需要重新扫描；返回的图谱 metadata 不含所有节点正文。

user 范围仍需 include_user:true；显式选择后，长期 preference 类型不要求与代码查询词相同，其余用户知识仍按 query 检索。所有记忆仍服从当前指令，不保证仅凭词法匹配即可判断适用性。context 的字符上限约束 text，JSON 元数据另行返回。

## 本地文件

记忆库含 `config.toml`、`projects/`、`tasks/`、`knowledge/`、`architecture/`、`index/memory.sqlite3`、`runtime/`。任务使用一份权威 Markdown，正文保留检查点历史；长期知识修订替换当前正文。frontmatter 是 JSON 映射形式的有效 YAML 子集，手动编辑时需保留字段和 JSON 格式。支持 UTF-8 与 CRLF，单文档最多 256 KiB。

Markdown 是记录权威；SQLite 同时保存可重建索引和**不可随意删除的操作日志与遗忘墓碑**。备份整个记忆库，服务停止后复制最简单；在线备份需正确处理 SQLite WAL。不要只复制数据库主文件或把整个 index 目录当作缓存清空。

受控写入使用跨进程锁及准备日志，再原子替换文件；重启时根据前后指纹恢复。记录级 revision 与 etag 同时匹配才可更新，手动修改正文也会改变 etag。外部编辑器不遵守 MemWeave 的锁，使用时避免同时保存同一文件；检测和替换之间极短的系统调用窗口不能提供跨编辑器的绝对原子比较保证。断电后的磁盘级持久性尚未做硬件故障验证。

`rebuild` 从文档重建记录索引，保留操作历史和墓碑。扫描文件索引丢失后重新扫描。遗忘清除当前文档和逻辑索引，并防止旧请求复活相同 ID；不承诺取证级安全擦除，也不会改写外部备份或已导出的内容。

## 中断与冲突恢复

正常中断运行 `memweave recover --home <库>`，或重新启动服务即可恢复。若文件同时被外部修改，返回 `RECOVERY_REQUIRED`，保留当前内容并暂时阻止普通记录访问。

1. 停止编辑并备份整个库。用 `call recovery_status --input '{}'` 查看操作、路径、当前/预期指纹与 proposed_record。
2. 若决定完成原操作，先把当前外部版本另存到库外，再将目标文件修复到原版本或 proposed_record 对应的 Markdown，然后运行 `recover`。不要直接改数据库。
3. 若决定保留当前文件，确保它是有效 Markdown，保留原 ID；把状态中的每个路径与 current_etag 放入 expected_files，调用 `recovery_resolve`。它放弃该笔未完成写入、保留当前有效内容并重新索引。文件已删除时值为 null；删除会留下墓碑。
4. 文件若再次变化，解决操作会报版本冲突，需重新检查。成功返回 abandoned:true；旧请求重试也返回放弃结果，不会重新覆盖文件。

## HTTP

`ui` 只监听 127.0.0.1，启动输出带令牌的 URL。页面将令牌移到 sessionStorage 并清理 URL。API 使用 Bearer 头，校验 Host、Origin 与跨站标识；不提供跨域访问。GET `/api/status`，其余 POST `/api/{action}`。JSON 对象最大 256 KiB；请求内容与令牌不写访问日志。会话令牌仅保护本地 HTTP API，不替代操作系统账户隔离。
