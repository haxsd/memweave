# 实现参考与融合决策

> 2026-10-27 方向变更：MemWeave 选择 AGPL-3.0-or-later 完全开源。上游采用边界必须明确区分：**Basic Memory 是源码级 fork**；**planning-with-files 是行为与 Skill 级改编，未复制其脚本，也不运行第二套计划文件系统**。Basic Memory 已接入单一运行时；planning-with-files 的计划、发现、进展、失败、验证与恢复规则已改编进统一任务、WorkUnit、阶段记录和连续性 Skill。

## 上游基线

| 来源 | 固定提交 | 许可证 | 目标使用方式 | 义务与边界 |
|---|---|---|---|---|
| [planning-with-files](https://github.com/OthmanAdi/planning-with-files) | `dcdfb278de53fdb69b2067ea173dbb1de45345a3` | MIT | 改编任务计划、发现、进展、恢复、Stop 维护和 Skill 生命周期规则；不复制其脚本 | 保留版权与 MIT 许可声明；记录改编范围；不把上游计划正文当成可执行指令 |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | `438d49776b8682f0a561bb3dc66092a33a5a829b` | AGPL-3.0-or-later | 直接 fork/改编 Markdown 存储、SQLite 派生索引、搜索、上下文、近期活动和关系能力，作为 MemWeave 统一底座 | MemWeave 整体按 AGPL-3.0-or-later 发布；保留来源、修改记录与源码获取方式；不提供闭源衍生发行 |

固定提交是 2026-10-27 的实现核对基线。Phase 0 的 `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 已建立；Basic Memory 的实际导入树与 planning-with-files 的改编文件都记录在第三方声明中，后续同步上游时先更新清单。

## 融合决策

- 最终产品只有一个 MCP、一个 Skill、一个 Hook 入口。两个 MCP 服务、两个索引或两个上下文 builder 属于简单拼接，不是目标。
- Basic Memory 负责底层 Markdown/SQLite、检索、上下文和关系能力；MemWeave 不再维护一套更弱的同类引擎。
- planning-with-files 负责任务生命周期和行为纪律；任务状态进入 MemWeave 的统一记录，而不是并排保留一份外部计划系统。
- 任务工作记忆与长期知识共享项目身份、存储和关系；文件/架构关系只作为定位证据，不是第三层产品。
- 同项目单 current 顶层任务，其余任务顺序排队；current 内使用多个持久 WorkUnit 并行执行子代理工作，不沿用 0.2.5 的“多个活跃任务再选择”模型。
- 宿主 Agent 继续负责理解、提炼、去重和纠正；MCP、Hook 和安装器不独立调用模型。

目标行为与执行顺序分别见 [AGPL 深度融合目标规格](specs/2026-10-27-agpl-fusion-target.md) 和 [PLAN.md](../PLAN.md)。

许可证文本、完整来源记录和修改清单见仓库根目录 [LICENSE](../LICENSE) 与 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。Basic Memory `0.24.1` 已接入单一服务生命周期；planning-with-files 固定提交的规则已改编进 MemWeave 自有记录模型，未作为第二套产品运行。

上游采用程度（2026-10-28 核对）：Basic Memory 的 `src/basic_memory/` 有 546 个文件；归一化 Windows CRLF 后，542 个文件与固定提交完全一致，只有 4 个文件包含已声明的文本检索适配修改，因此是源码级 fork。planning-with-files 的 22 个脚本一个也没有复制，`task_plan.md`/`findings.md`/`progress.md` 文件系统、plan-hash attestation、`check-complete` 和 `gate-stop` 也没有原样实现；MemWeave 用 task/WorkUnit/phase 记录、`task_close` 验收门禁和单一 Hook 入口表达相近约束。不得把后者写成“复制了 planning-with-files 的实现”。

## Phase 1–2 实际接入

- `src/basic_memory/` 是固定提交的上游源码树；`src/memweave/basic_memory_engine.py` 和 `basic_memory_index.py` 是在其上的单进程、单项目适配层。
- MCP 的 lifespan 启动同一个 `Service` 和唯一 Basic Memory index；不再启动 Basic Memory MCP、HTTP、CLI、cloud 或第二个检索服务。
- 业务记录仍以 Markdown 为权威；`index/basic-memory/memory.db` 是可重建的 FTS/上下文索引。旧的 `records` 表与自研词法搜索已删除。
- Phase 3 已统一知识检索与上下文来源：`memory_search` 返回 Basic Memory FTS 命中、permalink、score 与当前 Markdown revision/etag；`context_get` 不再使用近期背景兜底，并附带 Basic Memory context 的关系/observation 覆盖。最终文字仍按任务优先预算由 MemWeave 组装。
- 活动库从开发区的 `work/runtime/memory home/` 迁到 `%USERPROFILE%\.memweave`，客户端配置和项目安装清单同步更新。
- planning-with-files 的脚本没有原样打包；其计划、发现、进展、失败、三次升级、验证和恢复纪律已进入任务记录、WorkUnit 和连续性 Skill。
- Phase 2 把顶层任务改为单 `current` 与确定 `queued` 顺序，关闭当前任务时原子推进下一项；current 内 WorkUnit 使用独立 revision/etag，可由多个子代理或 MCP 进程并行更新。

以下 0.2.5 行为保留为迁移与历史对照，不再代表当前知识检索后端：

- 唯一进行中的任务在 `SessionStart` / Cursor `sessionStart` 自动成为恢复候选；多个任务只列候选。Cursor `sessionStart` 的输出是 fire-and-forget 的 `additional_context`，不能把 Hook 收据当成模型已收到；Phase 14 在 3.27.22 上按模型可见上下文验证，实际恢复仍由常驻 MCP 首轮 `workspace_prepare` 保证。
- Codex `UserPromptSubmit`、`PostToolUse` 和 Cursor `afterMCPExecution` / `afterFileEdit` 曾用于逐轮召回、任务关联和待保存标记；Phase 12 已将它们从默认安装移除，避免每次工具调用或文件编辑都启动独立进程。原话检索和任务绑定改由常驻 MCP 完成。
- 默认生命周期 Hook 只保留 SessionStart、结束清理和中断；Stop follow-up 已移除。待审查、阻塞 WorkUnit 和阶段状态通过下一次 SessionStart 或显式 MCP 读取恢复，避免每轮插入维护轮。
- Skill 要求先核对恢复候选与当前目标，再用 Git 状态和实际文件验证旧结论。
- 小型知识库没有强匹配时仍保留近期背景兜底；它不代表历史知识已经适用。

历史证据保留在 `work/evidence/2026-10-27-hook-continuity/`。

## 设计吸收矩阵（Phase 7）

下表只记录已经理解并能映射到当前产品的设计。`保留` 表示当前实现已经正确；`吸收` 表示本轮直接实现；`改造` 表示保留目标但改变承接方式；`拒绝` 表示会破坏单运行时或制造第二套真相。

| 上游机制 | 解决的真实问题 | MemWeave 当前等价物 | 决定 | 优先级 |
|---|---|---|---|---|
| Basic Memory：Markdown 权威，SQLite 派生 | 业务记录不能被索引或缓存绑架 | 活动库、重建测试、Markdown 正文 | 保留 | 核心 |
| Basic Memory：增量索引、校验和、重建 | 来源变化后索引陈旧或全量重扫 | `BasicMemoryEngine.index_vault`、checksum、rebuild | 保留并继续测试 | 高 |
| Basic Memory：FTS + 元数据过滤 + ContextService 关系/observation | 召回不能只有扁平关键词 | `memory_search`、`memory_context`、有限的 context 覆盖 | 保留，后续扩展关系覆盖 | 中 |
| Basic Memory：schema/Alembic 迁移 | 长期记录需要可演进 | vendored Alembic 与运行时 schema | 保留 | 核心 |
| Basic Memory：可选语义 provider | 语义召回与成本/依赖之间的取舍 | 当前显式 `none`，仅文本 FTS | 拒绝默认启用；以后按真实收益评估 | 中 |
| Basic Memory：API/CLI/cloud/多服务入口 | 上游平台能力完整 | 只保留一个 MCP 和本地 CLI | 拒绝作为产品表面 | 低 |
| planning-with-files：2-action、read-before-decide、update-after-act、记录错误、失败不重复、3-strike | 长任务丢失证据、重复失败、计划漂移 | 单一 continuity Skill、检查点、失败/验证字段 | 保留并强化提示 | 核心 |
| planning-with-files：5-question reboot | 新会话恢复慢、必须重新询问 | 原先只有原始任务/历史文本 | **本轮吸收**为结构化 reboot summary | 最高 |
| planning-with-files：completion/stop gate | Agent 把未完成阶段或缺失验收当成完成 | `task_close` 已有验收/WorkUnit 门禁；completion readiness、attention 和下一次 SessionStart 恢复待办 | **本轮改造**为显式 MCP 查询与任务边界维护，不注册逐轮 Stop follow-up；close 仍要求 host-reported acceptance | 高 |
| planning-with-files：plan attestation、active plan、ledger | 计划被静默改写、恢复目标不明、证据难追 | 单 current、revision/etag、检查点历史；Phase 8 增加 goal+acceptance contract digest | 已部分改造：用 task contract digest + `task_contract_drift`；显式 `attest_contract=true` 重新确认；不复制 plan 文件系统或 ledger | 中高 |
| planning-with-files：22 个宿主脚本、多宿主适配器 | 覆盖多种客户端 | 一个 Hook CLI，Codex/Cursor 适配 | 拒绝照搬 | 低 |

吸收原则：先理解问题和不变量，再在 MemWeave 现有记录模型里落最小改动。不能为了“像上游”而增加第二个索引、第二套计划文件、第二个 MCP 或多套上下文构建器。

## 许可证与来源执行要求

1. Phase 0 已完成 AGPL-3.0-or-later `LICENSE` 和包元数据。
2. 新增第三方声明，逐项记录上游项目、固定提交、许可证、复制/修改文件和 MemWeave 中的路径。
3. 直接复制的 MIT 文件保留原版权与许可文本；AGPL 文件保留来源与修改说明。
4. 保留上游 git 历史或可追溯的来源映射，避免把合并后的代码伪装成完全原创。
5. 每次同步上游时更新固定提交、diff 范围和验证结果。
6. 若未来出现闭源发行需求，必须重新选择架构或取得额外授权；不能继续使用当前融合代码路径。
