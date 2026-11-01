# MemWeave AGPL 深度融合实施计划

> 2026-10-29 Phase 14 结论：Codex 桌面新会话与真实压缩后恢复通过。Cursor `sessionStart` 的官方契约是 fire-and-forget；3.27.22 虽输出 `additional_context`，模型仍未收到启动块。两个 Cursor 原生会话（普通首轮、4 号检查点之后）都通过常驻 MCP + 项目 Rule/Skill 首轮 `workspace_prepare` 恢复，后一个读到 revision 5、4 个检查点和新的 next step。`hook_sessions.startup_context_emitted=true`（旧字段 `context_delivered`）不是模型交付证明。Cursor MCP 按项目/仓库启用一次。

> 唯一有效的长期执行计划。创建于 2026-10-27。当前开发树已完成 Phase 0–14；Phase 10 增加架构记录受 revision/etag 保护的更新与遗忘、来源漂移保护、tombstone 和 knowledge/architecture 状态计数，Phase 11 把项目级状态与架构纠正/遗忘接入管理页，Phase 12 完成长期项目的有界任务历史、独立检查点、自动归档与非打断式低频 Hook，Phase 13 将 MCP 收敛为单一通用 22 工具面并完成产品价值复核，Phase 14 在真实 Codex/Cursor 会话验证新会话、检查点和压缩恢复。真实活动库已清除旧演示架构造成的 `graph_stale`，管理页已在真实浏览器完成 stale → 显式重扫纠正 → 遗忘闭环。`github-radar` 的四任务顺序链路、真实 `task_close`、文件编辑和旧 Stop 证据保留为历史；Codex CLI 与桌面 App 已产生成功 `SessionStart`/`SessionEnd` 收据；Cursor MCP 首轮恢复已验证，但模型可见启动注入和 `preCompact` 仍无通过证据。0.2.5 私有开发快照已提交推送；公开标签和 GitHub Release 尚未执行。除本文件外，不再新增逐轮 plan/report。

**目标：** 把 MemWeave 做成 AGPL-3.0-or-later 完全开源、本地优先的编程 Agent 连续工作层。以 Basic Memory 的 Markdown + SQLite、检索与上下文能力为底层，直接融合 planning-with-files 的任务生命周期与 Hook 机制，最终形成一个 MCP、一个 Skill、一个 Hook 入口，而不是并排运行两个成熟项目。

**目标规格：** [AGPL 深度融合目标规格](docs/specs/2026-10-27-agpl-fusion-target.md)

**当前基线：** 分支 `main` 上的 0.2.5 私有开发快照。2026-10-29 Phase 14 全量回归 204 项运行，1 项仅因 Windows 符号链接权限跳过，其余全部通过（669.450 秒）；Phase 14 定向回归 35 项通过。Phase 0–14 已完成；Codex 与 Cursor 的受管入口统一为 `universal` 22 工具面，`clients doctor` 修复 5 个过期资源后复检 6/6 `ready`；Codex、Cursor 当前项目 `check --probe` 均为 SDK stdio、22 工具、1 个 active task。真实活动库 `workspace_prepare` 返回 1 个 current 任务、5 条近期历史，`attention` 为空；最终项目扫描覆盖 611 个文件、0 错误。`sessionStart` Hook 两次独立 CLI 实测 3.4–3.6 秒，输出约 3.9k 字符，但 Cursor 3.27.22 的 `additional_context` 没有进入模型可见上下文。Codex 新会话和真实压缩后恢复通过；Cursor 普通首轮和检查点后新会话通过常驻 MCP + Rule/Skill 恢复；Cursor `sessionEnd` 和统计收益仍未验证；默认安装不含 `preCompact`。

**完成标准：**

- MemWeave 以 AGPL-3.0-or-later 开源，第三方来源、修改和许可证义务可追溯。
- 一个 MCP 服务、一个 Skill、一个 Hook 入口，任务与长期知识共享同一存储、索引和上下文管线。
- MCP 只有一个默认通用工具面；不向用户暴露 `core/full` 或轻量/全量选择，诊断、恢复和管理动作走 CLI/UI 或内部验收路径。
- 每个项目只有一个当前任务，其余任务按顺序排队，不再按更新时间猜测。
- 任务工作记忆和长期知识两层职责清晰；文件与架构关系只作为定位证据，不是第三层产品。
- 三个任务连续性与一个长期积累场景通过真实客户端验收。
- 0.2.5 现有数据可以迁移或明确保留，不因融合丢失用户记录。

## 不可漂移的产品决策

1. **完全开源。** 许可证为 AGPL-3.0-or-later；Basic Memory 代码必须在 AGPL 边界内使用，planning-with-files 的 MIT 代码复制需保留声明。
2. **Basic Memory 是底座，不是并列服务。** 直接 fork、移植或改编其存储、索引、检索和上下文实现，不再自研一套更弱的同类引擎。
3. **planning-with-files 是任务生命周期核心。** 其计划、发现、进展、恢复和提交纪律进入 MemWeave 的统一任务模型；不是作为旁边的一份外部 Skill。
4. **单运行时。** 最终产品只有一个 MCP、一个 Skill、一个 Hook 入口。两个 MCP、两个索引、两个上下文 builder 属于失败形态。
5. **产品只有两层记忆。** 任务工作记忆与长期知识是两层；项目结构和架构关系是支撑两层的证据图，不是第三层。
6. **单活跃任务。** 同一项目按顺序推进任务：一个 `current`，其余 `queued`；关闭当前任务后才推进下一个。
7. **Markdown 权威，SQLite 派生。** 用户记录必须可读、可检查、可迁移；索引和缓存允许重建。
8. **宿主负责理解。** MemWeave 不独立调用模型；Hook 只触发、注入和提醒，不伪造检查点、总结或验收。
9. **任务串行，执行可并行。** 单 `current` 只约束同一项目的顶层任务顺序；当前任务内允许并行 WorkUnit 和子代理，各自独立记录负责人、范围、状态、发现与下一步。WorkUnit 只保证记录不互相覆盖，不隔离工作树；真实文件编辑须由宿主划分 worktree 或互不重叠的文件范围，父 Agent 负责合并与验收。

## 目标架构

```text
一个 MemWeave 运行时
├─ Markdown 项目记忆库
│  ├─ 当前任务、顺序队列、检查点、阶段
│  ├─ 长期知识与来源关系
│  └─ 项目结构与架构证据
├─ SQLite 派生索引（可从 Markdown 重建）
├─ Basic Memory 的检索与上下文管线（fork/改编）
├─ planning-with-files 的任务生命周期（MIT 移植）
├─ 一个 MCP 命名空间
├─ 一个 continuity Skill
└─ 一个 Hook CLI（Codex / Cursor 适配）
```

上下文组装采用统一优先级：

1. 当前任务目标、验收、最新检查点、失败尝试和下一步。
2. 与当前目标相关的长期知识与明确偏好。
3. 与当前文件和决策相关的结构/架构证据。
4. 来源、截断、覆盖和新鲜度说明。

任务状态、长期知识和结构证据共享同一个项目身份与关系索引。阶段提炼直接从任务证据进入知识层，不经过第二套数据库或手动复制。

## 四个真实验收场景

### 场景 1：同一 Agent 的长任务

一个复杂任务经历多次上下文压缩、多个阶段和多次验证。新上下文仍能取得任务目标、约束、已失败路径、当前进度和唯一下一步；不重复探索已否定方案。

### 场景 2：同项目新会话

任务做到一半关闭会话，在原项目打开新会话。Hook 恢复唯一当前任务并要求先核对用户目标和实际代码；不得把旧进度当成新目标。

### 场景 3：跨客户端接续

在 Cursor 推进任务，客户端不可用后改在 Codex 打开同一项目。新 Agent 读取同一任务状态、顺序队列和相关知识，接续工作；不依赖任一客户端的会话数据库。

### 场景 4：阶段后的长期积累

任务或项目阶段收束后，宿主基于证据提炼少量可复用知识，经去重、来源核对和显式审核进入长期层。下一次相关工作能取回它；无关工作不被注入。

## 阶段计划

### Phase 0：许可证、来源与基线冻结

**状态：已完成（2026-10-27）。**

- [x] 增加 AGPL-3.0-or-later `LICENSE`，明确 MemWeave 整体许可证。
- [x] 新增第三方声明，记录 Basic Memory 的 AGPL-3.0-or-later 来源、固定提交和修改文件；记录 planning-with-files 的 MIT 来源与复制/改编范围。
- [x] 固定两个上游项目的提交 SHA，保存 fork 关系、上游同步方式和许可证义务。
- [x] 更新 `pyproject.toml` 许可证元数据与仓库说明；确认不存在闭源发行路径。
- [x] 冻结 0.2.5 作为迁移基线；不为兼容旧结构继续扩张新功能。

**依赖：** 无。

**验证：** `LICENSE` 文本与 SPDX 标识正确；第三方声明覆盖所有直接复制或改编的代码；仓库说明不再把 Basic Memory 代码描述为“仅参考”。

**退出条件：** 法律和产品方向可执行，后续代码复制有明确归属。已满足。

### Phase 1：以 Basic Memory 为底座建立单运行时

**状态：已完成（2026-10-27）。** Basic Memory `0.24.1` 源码已按固定提交复制到 `src/basic_memory/`；主 MCP 的 lifespan 持有同一个 `Service` 与 Basic Memory index，旧 `records` 表、自研词法检索和第二个记忆服务已移除。活动库迁移到 `%USERPROFILE%\.memweave`，Markdown 保持权威，SQLite 派生索引可从 Markdown 重建。

- [x] 从固定上游提交 fork Basic Memory，保留来源、提交和修改记录。
- [x] 把 Markdown 存储、SQLite 索引、搜索、上下文与近期活动实现接入 MemWeave 包结构。
- [x] 移除与 Basic Memory 重复的自研存储、索引和检索路径；保留迁移适配层。
- [x] 确定项目记忆库位置：使用稳定的用户级记忆库 `%USERPROFILE%\.memweave`，产品数据不再位于 `work/`。
- [x] 用一个 MCP 进程启动，确认不出现第二个记忆服务。

**依赖：** Phase 0。

**当前验证：** 全套 `unittest` 153 项通过、1 项仅因 Windows 符号链接权限跳过。`tests.test_basic_memory_engine` 验证 Basic Memory 搜索与 context service 共用同一索引；`tests.test_basic_memory_index` 与 `tests.test_mcp` 删除派生 SQLite 后仍从 Markdown 重建搜索，真实 stdio 重启后任务、知识与探针可读。活动库 20 个文件逐文件哈希迁移一致，重建 16 条记录；`doctor`、Codex/Cursor `check --probe` 和 wheel 独立安装资源冒烟通过。上下文最终文字仍由 MemWeave 的预算化 builder 组织，当前 task/知识召回已走 Basic Memory FTS；完全统一 context builder 保留在 Phase 3，不能写成已完成。

**验证：** MCP stdio 启动、索引重建、搜索、上下文组合和重启恢复测试通过；停掉 MCP 时 Markdown 仍可直接读取。

**退出条件：** 单运行时底座可启动、可重建、可检查，不再维护两套后端。

### Phase 2：统一任务模型与顺序队列

**状态：已完成（2026-10-27）。**

- [x] 定义统一记录：项目、任务、内嵌检查点、WorkUnit、阶段、知识、来源和关系。
- [x] 任务状态固定为 `queued`、`current`、`completed`、`cancelled`；同一项目最多一个 `current`。
- [x] 在 `current` 任务下定义持久 WorkUnit；状态支持 `pending`、`running`、`blocked`、`completed`、`cancelled`，允许多个 WorkUnit 并行，子代理共享父任务 ID 但独立写入自己的进度。
- [x] 工作区恢复同时返回 `current` 任务与运行中/阻塞 WorkUnit；父 Agent 负责合并结果，WorkUnit 不得伪装成新的顶层任务。
- [x] 移植 planning-with-files 的计划、发现、进展、失败、验证和继续执行规则，写入统一任务记录。
- [x] 增加阶段边界；阶段收束可以触发知识提炼，但不必关闭整个任务。
- [x] 任务完成或取消后，按队列顺序推进下一个任务；不按最近更新时间猜测。
- [x] 任务关闭前要求必需 WorkUnit 已结束或被显式取消；父任务验收仍由宿主汇总，不由子代理自行完成。
- [x] 为 0.2.5 的活跃、完成和取消任务编写迁移与冲突处理。

**依赖：** Phase 1。

**当前验证：** 全套 `unittest` 164 项通过、1 项仅因 Windows 符号链接权限跳过。队列、并发 `task_open`、重复 `current` 迁移、关闭保护、阶段提炼和双 MCP 进程并行 WorkUnit 均有自动化测试；两个真实 stdio 服务进程可同时更新不同 WorkUnit，父任务 revision 不变。活动库 `%USERPROFILE%\.memweave` 已完成迁移，只有一个 current；18 条 Markdown 在线重建索引无失败，不依赖删除被其他进程占用的 SQLite 文件。

**验证：** 队列顺序、单 `current` 约束、并行 WorkUnit、子代理接管、父任务汇总、阻塞恢复、阶段边界、任务关闭与迁移测试通过。

**退出条件：** 任意时刻一个项目只有一个明确可恢复的顶层任务；该任务内部可安全并行多个 WorkUnit，阶段可独立收束。

### Phase 3：统一 MCP 接口与上下文管线

**状态：已完成（2026-10-27）。**

- [x] 设计最小 MCP 工具面：工作区准备、任务创建、检查点、阶段收束、任务关闭、知识搜索/写入/审核等，不保留重复入口。
- [x] 使用 Basic Memory 的检索与上下文能力，删除重复的词法评分和近期回退实现。
- [x] 统一预算、来源、截断和新鲜度语义；当前任务状态优先，长期知识和结构不得挤掉下一步。
- [x] 任务记录、知识记录与结构证据共享项目身份和关系查询。
- [x] 保留幂等、版本/etag、跨进程写入保护和出错恢复。

**依赖：** Phase 2。

**当前验证：** 全套 `unittest` 169 项通过、1 项仅因 Windows 符号链接权限跳过。默认 core 工具从 22 项收敛到 20 项，隐藏重复的 `task_list`/`memory_read`；`memory_search` 返回 Basic Memory FTS 命中、permalink、score、当前 Markdown revision/etag；`context_get` 使用 Basic Memory context 关系/observation 覆盖，无匹配不再注入近期背景。新增测试覆盖无兜底召回、任务/WorkUnit 优先预算、项目隔离、索引重建后上下文一致、默认工具面、真实 Hook+MCP 后续复用，以及跨进程读取等待锁并恢复 pending 写入。

**验证：** MCP/CLI contract 测试、索引重建后查询一致、任务优先预算、来源截断、并发写冲突和恢复测试通过。

**退出条件：** 一个命名空间覆盖完整工作循环，没有两个工具集争夺同一职责。

### Phase 4：一个 Skill 与跨客户端 Hook

**状态：已完成（2026-10-27）。**

- [x] 以 planning-with-files 的 MIT 生命周期为基础，重写单一 continuity Skill，明确恢复、执行、阶段收束、长期提炼和纠正。
- [x] 建立单一 `memweave hook` 入口，映射 Codex 与 Cursor 的启动、提示提交、工具后、压缩、停止、中断和结束事件。
- [x] SessionStart 恢复唯一当前任务；UserPromptSubmit 使用本轮原话做一次去重召回；不保存原话或 transcript。
- [x] 变更或验证后标记待检查点；Stop 只触发一次有界维护。
- [x] PreCompact 不伪造总结；依赖持续写入与恢复时重新注入可检查状态。
- [x] 保留父项目/嵌套项目隔离和旧任务目标核对规则。

**依赖：** Phase 3。

**当前验证：** 全套 `unittest` 170 项通过、1 项仅因 Windows 符号链接权限跳过。新增真实跨客户端集成测试：Codex 侧通过 MCP 创建任务、检查点和 WorkUnit 并由 Hook 绑定，SessionEnd 后 Cursor `sessionStart` 在同一 home 恢复同一任务、下一步与并行单元。Codex/Cursor 各事件、原话不落库、Stop 去重、嵌套项目隔离和 Interrupt/End 清理均有测试。`work/evidence/2026-10-27-hook-continuity/` 的原生 CLI 与真实模型证据保留并标注为 Phase 3 前历史；桌面触发和收益仍未验证。

**验证：** 确定性事件测试、真实 Codex CLI 模型测试、Cursor 事件契约测试、提示词不落库、嵌套项目不双注入。

**退出条件：** 一个 Skill 和一个 Hook 入口能驱动同一运行时完成三种任务连续性场景。

### Phase 5：长期知识提炼与下一次复用

**状态：已完成（2026-10-27）。**

- [x] 阶段或任务收束后，从任务证据生成待提炼状态；不自动把检查点当长期知识。
- [x] 用统一检索管线查重、修订和合并，保存来源、范围、适用条件与状态。
- [x] 不确定知识进入候选，显式审核后启用；支持纠正和遗忘。
- [x] 项目知识默认项目范围；用户偏好仅在明确选择时进入用户范围。
- [x] 下一次相关工作按目标和范围取回知识；无关工作不注入。

**依赖：** Phase 2、Phase 3。

**当前验证：** 全套 `unittest` 171 项通过、1 项仅因 Windows 符号链接权限跳过。新增场景 4 集成测试覆盖：阶段收束不关闭任务；候选知识在激活前不进入上下文；显式激活后同一任务与下一任务都能按相关工作查询取回；无关查询不注入；用更新前后唯一检索词确认纠正后旧结论不再命中；遗忘后不再取回。

**验证：** 阶段提炼不要求关闭任务；重复知识可合并；候选默认不可见；纠正后旧结论不再注入；下一次任务真实取回。

**退出条件：** 场景 4 通过，知识层不再只是“保存了记录”。

### Phase 6：迁移、清理、验收与开源发布准备

**状态：已完成（2026-10-28）。** 迁移审计、工具面/安装校准、文档一致性、四个真实验收场景、单轮有效收益对照、清理隔离和最终 wheel 独立验收均已完成。公开提交、推送、标签和 GitHub Release 尚未执行。

- [x] 迁移 0.2.5 的项目、任务和知识；无法迁移的记录明确保留或导出。
- [x] 删除被融合实现取代的重复模块、旧工具和旧文档；宿主拒绝删除的可再生目录与外部引用目录已隔离并记录精确清单。
- [x] 完成四个真实场景验收，记录客户端版本、轮次、重复解释、误命中、恢复证据和剩余限制。
- [x] 对无 MemWeave 基线、仅任务记忆、完整任务+长期知识做同模型、同 fixture 的一轮有效对照；记录客观指标、隐藏验收结果和“单次样本、非统计 benchmark、不含 Cursor”的限制。
- [x] 校准工具面、默认上下文预算和安装流程；不以工具数量或安装成功作为产品完成标准。
- [x] 完成 AGPL、第三方声明、README、API、接入、开发说明和状态文档的一致性检查。
- [x] 在最新代码和文档上重建最终 wheel，并完成独立安装、资源、许可证、CLI 和 MCP 冒烟。

**依赖：** Phase 4、Phase 5。

**当前验证：** 171 项 unittest 通过、1 项仅因 Windows 符号链接权限跳过；旧备份 18 条和退役目录 16 条业务记录全部可在活动库中找到，活动库保持一个 current 和 Markdown 权威。15 个活动文档的本地链接检查缺失为 0，`git diff --check` 和 `pip check` 通过。Codex CLI `0.164.0-alpha.11.1` 完成长任务、新进程恢复和阶段知识复用，每场景 1 轮；Cursor `3.27.14` 桌面在场景 3 用 1 轮将真实任务 revision 6 写到 7，全新 Codex CLI 用 1 轮读回 revision 7，随后 Stop Hook 检查点写到 revision 8。四个场景均未要求用户再次解释目标；场景 3 记录了与同名全局服务和工作区选择相关的 2 次误命中，详见 `work/evidence/2026-10-27-phase6-clients/`。有效单轮 benchmark 中，A（详细需求基线）与 C（完整记忆）通过隐藏验收，B（仅任务记忆）因未恢复精确范围错误策略而失败；C 命中预置知识但比 A 更慢且 token 更多，因此只证明精确知识可召回，不宣称效率收益。最终 wheel 已重建并通过全新 venv 安装、`--version`、`pip check`、`init`、`doctor`、stdio `probe` 重启读回、42 个 Alembic 版本、2 个提示模板、1 个 MCP resource、6 个 MCP UI 页面、1 个静态页面、1 个 Skill 和 3 份许可证文本检查。更早候选 wheel 仅作为 superseded 证据保留。`build/` 与 `.build-wheel/` 的物理删除被执行策略拒绝；`work/runtime/memory home/` 仍被外部项目配置引用，三者均已隔离并记录。未验证：Codex 桌面 Hook 信任、Cursor 自动 Hook 触发、统计收益。

**验证：** 全套测试、四个真实场景及其客户端/修订证据、0.2.5 数据迁移、单 MCP/Skill/Hook 检查、许可证与链接检查、单轮公平对照、最终 wheel 独立安装和 MCP 重启。证据边界与未验证桌面 Hook 行为分别记录。

**退出条件：** 已满足。MemWeave 具备 AGPL 开源发布所需的代码、许可证、资源、文档与 wheel；公开提交和发布动作仍需用户明确要求。

### Phase 7：吸收上游设计，增强接续与门禁

**状态：已完成第一批（2026-10-28）。** 上游采用边界已经核对：Basic Memory 是源码级 fork；planning-with-files 是行为与 Skill 级改编，22 个脚本、独立计划文件和 attestation/check-complete 没有复制。Phase 7 只吸收能改善真实接续的设计，不引入第二套计划文件系统或第二个 MCP。

- [x] 在 `docs/design-references.md` 建立设计吸收矩阵，逐项写明保留、吸收、改造或拒绝及优先级。
- [x] `workspace_prepare` / `context_get` 返回五问 reboot summary：当前位置、目标、已完成证据、未解决失败和唯一下一步。
- [x] 返回机器可读 `attention` 与 `completion`：覆盖阻塞 WorkUnit、缺失下一步、待提炼项、过期/未检查的图谱证据和完成前阻塞。
- [x] 更新 API、Agent 指引、接续状态和 README，明确新字段与“不能把 readiness 当作完成验收”。
- [x] 完成针对性测试和全量回归；记录真实 MCP/CLI 输出证据。

**依赖：** Phase 2、Phase 3、Phase 4。

**验证：** 31 项针对性测试通过；全量 `unittest` 174 项通过、1 项仅因 Windows 符号链接权限跳过；全新 CLI `workspace_prepare` 返回 reboot、attention、completion，五问摘要能在小预算下保留下一步；阻塞 WorkUnit、缺失下一步、图谱过期和待提炼项均可由结构化字段发现；`task_close` 仍要求宿主逐项填写验收结果；不新增工具、服务、索引或计划文件。证据见 `work/evidence/2026-10-28-phase7/`。

**退出条件：** 新上下文字段有测试和文档；真实工作区准备输出包含可读摘要与可机器处理的 attention/completion；未把 readiness 写成完成，也未把未运行的验证写成通过。

### Phase 8：任务契约 attestation 与漂移警告

**状态：已完成（2026-10-28）。** 吸收 planning-with-files 的 plan attestation 思想，但不引入 `task_plan.md` 文件系统或第二套计划服务。任务契约是现有 goal+acceptance 的摘要，不是新的真相来源。

- [x] `task_open` 记录 goal+acceptance 的 contract digest 与确认时间；已有任务显示 unattested，不伪造已确认。
- [x] `task_checkpoint` 增加可选 `attest_contract=true`，只有显式确认外部契约变化后才刷新 digest。
- [x] `context_get`/`reboot` 返回 contract 状态，attention 增加 `task_contract_drift` 与 `task_contract_unattested`。
- [x] 更新 API、Agent/Skill 文档和 Phase 8 证据；运行全量回归。
- [x] 用 `setup` 同步当前 Codex/Cursor 项目资源和配置，完成两侧 `check --probe`；记录桌面 MCP 重载边界。

**依赖：** Phase 2、Phase 3、Phase 7。

**验证：** 32 项针对性测试通过；全量 `unittest` 175 项通过、1 项仅因 Windows 符号链接权限跳过；新建任务有 contract digest；外部改动 goal/acceptance 后出现 drift；显式重新 attest 后恢复 attested；`completion=ready` 仍不代替验收；当前项目 Codex/Cursor 与 D:\project Cursor 的 `check --probe` 均通过；不新增工具、服务、索引或计划文件。证据见 `work/evidence/2026-10-28-phase8/`。

**退出条件：** 契约状态有测试和文档；本地客户端资源已同步；当前桌面 MCP 重载边界明确；未把未确认的外部修改静默接受。

### Phase 9：客户端巡检、显式修复与真实桌面验收

**状态：客户端巡检、真实桌面任务生命周期和管理页验收已完成；成功 `sessionEnd`、`preCompact` 与统计收益保留为未验证边界（2026-10-28）。** 2026-10-28 重启 Codex 后发现，另一个已打开的 Codex 项目仍通过项目级旧配置启动退役记忆库；本阶段用 `clients doctor` 补齐了全机巡检与显式修复。Cursor `D:\project` 已真实收到 `sessionStart` 附加上下文、显示产品状态行并创建任务；`github-radar` 随后连续完成四个顺序任务，扁平参数兼容后的真实 `task_close`、知识审查、`afterFileEdit`、`afterMCPExecution` 和 `stop` 均成功。结束与压缩事件没有成功样本，不能写成已经完整验收。

**记忆边界补充（2026-10-28）：** MCP `memweave_memory_write` 默认生成 `candidate`，不会把 Agent 的阶段记录直接升格为 active 长期知识；project/task scope 在 `context_get` 和 `memory_search` 中按项目/任务过滤，user scope 必须显式 `include_user=true`。新增跨项目上下文回归和默认 candidate 回归，避免“共享一个 home”被误解为“所有项目共享事实”。

- [x] 新增 `memweave clients doctor`，默认用活动库登记的精确项目根目录和 Codex 已知可信项目组成扫描集合；`--root` 可追加未登记项目，`--client` 可限定 Cursor、Codex 或两者。
- [x] 巡检同时报告全局与项目级入口，识别旧 home、旧版本、解释器/服务命令失效、MCP 或 Hook 缺失、受管 Skill/Rule 漂移、项目未登记，以及全局和项目入口指向不同服务时的重复/遮蔽风险；不把配置成功写成桌面验收。
- [x] 提供显式 `--fix`，仅对项目级受管安装复用既有 `setup` 和备份机制；配置无问题时不得写文件，全局配置只报告、不擅自修改，受管文件被人工改动时继续拒绝覆盖。
- [x] 自动化测试覆盖健康安装、旧 home 默认扫描、显式根目录、全局与项目冲突、资源漂移、无安装和无问题修复幂等；目标测试与全量回归通过。
- [x] 更新 CLI/API、接入、README、当前状态和安装资源说明；同步本机四套 Codex/Cursor 配置并记录当前代码的真实桌面事件、注入证据、客户端版本和仍需人工触发的场景。

**依赖：** Phase 4、Phase 6、Phase 8。

**验证：** `tests.test_hooks`、`tests.test_setup` 和 `tests.test_client_doctor` 共 41 项通过；核心 MCP/Hook 目标回归 12 项通过；全量 `unittest` 192 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 645.635 秒。真实活动库扫描 6 个已配置入口 ready、0 needs-attention；本轮 Skill 同步覆盖当前项目 Cursor/Codex、`D:\project` Cursor、`haxsd-byok` Codex、`github-radar` Cursor 和全局 Codex Skill，均保留备份。管理页已在真实浏览器验证 project/task/user 范围、candidate/active 筛选、任务范围和项目检索，控制台无错误。Codex 当前桌面回合观察到 Hook 注入，Codex 收据覆盖七类事件。Cursor `3.27.22` 的 `D:\project` 当时观察到启动输出、真实状态行和任务检查点；`github-radar` 已完成四个顺序任务，`task_d374` 与 `task_fd174` 均为 completed 且知识审查 reviewed，`afterFileEdit` 在真实业务文件和测试文件上 exit 0，`afterMCPExecution` 与 `stop` 多次成功。成功 `sessionEnd`、`preCompact` 尚无样本。证据见 `work/evidence/2026-10-28-phase9/`。

**退出条件：** 新命令可在本机已知项目上完成只读巡检和显式修复；客户端配置漂移有可重复测试；当前代码在 Codex/Cursor 桌面的事件、注入、宿主使用、真实关闭和文件编辑有证据记录；未新增第二套 MCP、索引、计划文件或全局写配置路径。上述条件已满足；成功结束/压缩事件保留为明确未验证边界，客户端 `desktop_verification` 仍不得由配置自检改成通过。

### Phase 10：架构记录生命周期与状态可观测性

**状态：已完成（2026-10-28）。** 架构证据此前只能新建，无法纠正或遗忘；一条旧演示记录因此永久触发 `graph_stale`。本阶段让架构证据具备与长期知识相同的版本化维护入口，并增加可解释的 scope/status 计数，不改变 Markdown 权威、单 MCP/Skill/Hook 或任务队列模型。

- [x] `graph_query` 的架构节点返回 revision、etag、sources、stale_reasons 与当前状态。
- [x] 新增 `graph_update` 与 `graph_forget`，要求项目范围、revision/etag 和当前来源哈希；更新与删除使用现有原子提交和 tombstone。
- [x] `system_status` 增加 knowledge 的 scope/status 聚合和 architecture stale/unverified 聚合；Phase 10 当时默认 core 与 full 工具数为 22/31，Phase 13 已收敛为单一通用 22 工具面。
- [x] 自动化测试覆盖更新、来源漂移、版本冲突、跨项目拒绝、遗忘、tombstone 防复活和状态计数。
- [x] 真实活动库通过 `graph_forget` 删除旧演示架构记录；文档定稿后执行 `project_scan` 清零文件 stale。
- [x] 同步 API、Agent guide、Skill、接入、README、CURRENT_STATE 与本机受管资源。

**依赖：** Phase 3、Phase 7、Phase 9。

**验证：** 全量 `unittest` 196 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 617.015 秒；真实 `graph_query` 返回 revision/etag/stale_reasons，`graph_forget` 删除旧演示记录，tombstone=1 且 query 不再返回；状态计数与工具面测试通过。最终 `project_scan`、客户端 tool_count、Skill 哈希和 wheel 证据记录在 `work/evidence/2026-10-28-phase10/`。

**退出条件：** 架构过期记录可通过产品入口纠正或遗忘并清除 stale 告警；状态计数与查询/记录事实一致；无第二套计数或图谱引擎；全量测试和本机资源同步通过。

### Phase 11：管理页架构维护与项目记忆状态

**状态：已完成（2026-10-28）。** Phase 10 已提供安全的架构更新和遗忘后端，但管理页仍只能查看节点，无法查看 revision/etag、来源漂移原因或执行纠正。本阶段已把能力接到用户实际可操作的项目页面，并让当前项目显示与全局汇总同源的 scope/status 和架构 stale 计数。

- [x] `status` 支持可选 `project_id`，返回当前项目知识 scope/status、任务队列和架构 total/stale；全局字段继续保留，统计复用同一 Markdown 与扫描事实。
- [x] 项目总览展示当前项目记忆状态，不再用最多 20 条检索结果冒充项目总量。
- [x] 图谱页可查看 stale 架构的 revision、etag、来源、关系与过期原因，并从节点详情发起纠正或遗忘。
- [x] 纠正沿用 expected revision/etag；来源漂移必须显式重新扫描并确认当前哈希，不能静默接收；遗忘要求输入确认词并调用 `graph_forget`。
- [x] 自动化测试覆盖项目状态投影、Web 架构生命周期、错误显示契约与现有图谱测试；定向及全量回归通过。
- [x] 更新 API、README、接入/当前状态文档，完成页面真实浏览器冒烟和本机入口同步；保留不能用配置成功率自证的桌面边界。

**依赖：** Phase 3、Phase 10。

**验证：** 56 项定向测试运行、1 项仅因 Windows 符号链接权限跳过且其余通过；全量 `unittest` 197 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 612.127 秒；内联 JavaScript `node --check` 与 `compileall` 通过。真实宿主内嵌 Chromium 在隔离临时库完成：项目状态同源显示、stale 架构详情、未确认来源漂移被本地阻止、显式重扫后 revision 1→2、确认词错误被拒绝、`graph_forget` 后节点消失、Markdown 删除与 tombstone 写入。真实活动库当前 24 条知识（15 active / 9 candidate）、architecture total=0/stale=0；6 个客户端入口 ready，均指向当前源码 `.venv` 与共享 home。证据见 `work/evidence/2026-10-28-phase11/`。

**退出条件：** 已满足。用户能在管理页从当前项目状态定位到 stale 架构，核对版本与来源，完成一次受控纠正或遗忘，并看到同一统计随之更新；没有新增第二套 MCP、图谱、索引或统计数据库。

### Phase 12：长期项目的工作批次与有界历史

**状态：实现与自动化验收完成（2026-10-29）；Codex CLI 与桌面已产生 SessionStart/SessionEnd 收据，Phase 14 进一步验证了 Codex 新会话/压缩恢复与 Cursor MCP 首轮恢复；Cursor 模型可见启动注入仍未通过。** Phase 0–11 是 MemWeave 自身的开发路线图，不是用户项目的数据模型。用户项目按月或按需求批次推进时，不产生累计的 `Phase 12/13/...`；每个有明确目标与验收的工作批次仍由现有任务生命周期承载。一个批次可以包含一个 current 任务和若干 queued 任务。完成并审查的任务进入归档历史，不自动删除。

- [x] 修正长期项目语义：文档和接口明确区分开发路线 Phase 与用户项目工作批次，不把开发阶段写入用户项目上下文。
- [x] `workspace_prepare` 只装配 current、queued、待知识审查项、计数和很小的近期归档切片；默认路径不得读取全部任务正文。
- [x] `task_list` 改为默认可用的有界历史入口，支持状态、标题/目标/摘要查询、limit/offset 和总数；归档已完成审查任务与已取消任务，默认不混入活动队列。
- [x] 检查点改为独立 Markdown 记录；任务正文和阶段收束不再无限追加。`task_read` 对 phases、checkpoints、WorkUnits 返回有界摘要与总数，并支持显式分页读取历史。
- [x] 统一 Hook 热路径：Codex、Cursor 默认只注册会话生命周期事件；文件编辑、工具调用和每轮原话召回改走常驻 MCP，`setup` 清理旧的高频受管事件，不提供“轻量版/全量版”双轨。
- [x] 活动库迁移保持原始 Markdown 不丢失；真实项目验证长期队列、归档检索和恢复路径，运行定向与全量回归。
- [x] 更新 API、README、Agent guide、continuity Skill 与 CURRENT_STATE；同步本机客户端资源，并记录需要重载 MCP 才能使用新接口的边界。

**依赖：** Phase 2、Phase 3、Phase 5、Phase 7、Phase 9。

**验证：** 25 个任务与 25 个检查点的长历史测试覆盖：默认上下文的大小不随历史线性增长；旧任务仍可显式分页检索；归档不删除 Markdown；关闭、审查、恢复和队列推进保持原语义；阶段收束不追加任务正文。Phase 12 收尾时真实活动库为 23 个任务（1 current / 22 archived），默认 `workspace_prepare` 只返回 1 条 current 与 5 条近期归档，`task_counts.total=23`；Phase 13 审计任务加入后当前为 24 个（1 current / 23 archived）。Phase 12 收尾时全量 `unittest` 203 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 636.881 秒；安装资源/工具面定向回归 26 项通过。`clients doctor` 会识别受管 Skill/Rule 落后于当前包，修复 5 个旧入口和 3 个过期 Cursor 资源后 6/6 ready；五份项目 Hook 配置均只保留低频事件，Skill/Rule 与当前源码一致。

**退出条件：** 一个持续数月或数年的项目可以反复出现和暂停需求批次，而默认 MCP 上下文只包含当前工作集与明确计数；需要旧历史时可按需查询，不依赖把全部记录塞进 prompt。

### Phase 13：单一通用工具面与产品价值复核

**状态：已完成（2026-10-29）。** 用户明确否定了轻量/全量或 `core/full` 双轨：产品应给 Codex 和 Cursor 一个合适的默认通用面，而不是把选择和配置成本转移给用户。Phase 10/12 中“22/31 双工具面”的描述作为历史实现记录，当前产品行为以本节为准。

- [x] 将 MCP 收敛为一个默认通用工具面：保留日常任务、知识、结构证据和上下文入口；诊断探针、队列恢复、项目列表、完整上下文读取和管理式架构遗忘不向 Agent 暴露。CLI/UI 仍保留运维和受控管理能力。
- [x] 移除用户可见的 `--toolset`/`core/full` 配置与安装清单双轨；旧配置由 `setup`/`clients doctor --fix` 迁移。为旧进程保留只读兼容参数时，不得改变工具目录或让用户再次选择模式。
- [x] 更新 MCP、CLI、安装器、系统状态、测试和文档，使工具数量、名称和能力只有一个真相来源；`probe` 使用内部诊断路径，不改变用户可见目录。
- [x] 复核最初三条短期连续工作场景与长期知识场景：长任务恢复、同项目新会话、Cursor→Codex 跨客户端接续，以及候选知识提炼/激活/复用；记录真实可观察结果和边界。
- [x] 审计 Hook、上下文和工具面的体验/性能：默认只有低频生命周期事件；上下文有界；不能用逐轮维护、重复注入或大工具清单制造新的停顿和成本。
- [x] 完整回归、安装巡检和客户端重载验证；更新 README、API、Agent guide、Skill、CURRENT_STATE 与证据，明确“已验证”“仍需观察”和“未验证”三类结果。

**依赖：** Phase 12。

**验证：** 全量回归 203 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 598.362 秒；60 项工具面、MCP、安装、客户端巡检、Hook 和长历史定向回归通过。默认连接与旧 `--toolset full` 兼容调用得到同一 22 工具集合，内部 diagnostic 路径保留 31 项并完成真实 stdio 重启读回。5 个本机受管入口已迁移为 `universal`，`clients doctor` 6/6 `ready`，Cursor/Codex `check --probe` 均返回 22 工具且配置不含 `--toolset`。真实活动库 `workspace_prepare` 冷启动约 1,729ms、上下文约 6.9k 字符、24 个任务（1 current / 23 archived）、1 + 5 条任务页、`included_sources=30`、图谱扫描 611 个文件且无过期文件，`attention` 为空。真实 `sessionStart` Hook 两次独立 CLI 实测 3.44–3.63 秒，输出约 3.9k 字符；默认 Hook 只在会话生命周期触发，不逐轮维护。Codex CLI 与桌面应用均已产生 `SessionStart`/`SessionEnd` 收据；Cursor 原生 Agent 产生 `sessionStart` 输出，但 Phase 14 的模型可见验证进一步确认启动块未送达，不能把 `startup_context_emitted` 或旧 `context_delivered` 当成交付证据；Cursor `sessionEnd` 和统计收益仍属未验证边界；`preCompact` 未安装。

**退出条件：** 用户不需要理解或选择工具模式；Codex/Cursor 都连接同一个通用 MCP；产品日常使用只受任务、知识和客户端生命周期影响，不再受模式拆分或高频维护折磨。

### Phase 14：真实使用与价值验证

**状态：已完成（2026-10-29）。** 不新增运行时、模式或 Hook 事件；用真实项目和新会话验证现有能力是否真的可用，并把摩擦转成下一步的窄修复。

- [x] 建立 Phase 14 基线和真实 current task；Codex 桌面新会话复述出正确任务 ID 与 next step。
- [x] Cursor 原生 Agent 在项目级 MCP 启用后，用未提供 task ID 的普通提示自动调用 `system_status` / `workspace_prepare`，正确恢复 `task_3ef788908c6548ef9500fd68c6a00a34`、revision 4 和 next step。启动上下文仍为空；`startup_context_emitted`（旧 `context_delivered`）没有作为模型交付证据。
- [x] 写入第 4 个检查点后新建 Cursor 原生会话 `e54c5202-1a31-4099-ae81-a12d65f29679`，普通提示自动恢复到 revision 5、4 个检查点和新的 next step。Codex 当前线程发生真实上下文自动压缩，压缩后仍读回同一任务；Cursor 没有触发压缩，按未触发记录。
- [x] 汇总真实摩擦并更新文档：Hook 启动 CLI 约 3.4–3.6 秒；Cursor 恢复不再要求用户重述，但首轮模型为回答“做到哪/下一步”额外读取 PLAN/CURRENT_STATE 并做多次搜索，两次真实首轮分别耗时 1m16s 和 1m31s。该时间不归因于 Hook，而是 Agent 首轮探索路径过重，进入下一阶段优化。
- [x] 全量回归 204 项运行、1 项既有 Windows 符号链接权限跳过、其余全部通过（669.450 秒）；Phase 14 定向 35 项通过；`clients doctor` 复检 6/6 ready；最终项目扫描 611 文件、0 错误。

**当前发现：** Cursor 官方文档确认 `sessionStart` 是 fire-and-forget，`additional_context` 的目标是“initial system context”，但 3.27.22 实测在两个真实 chat 中都没有进入模型可见上下文；Hook 收据只能证明进程输出。实际可重复的恢复路径是常驻 MCP + 项目 Rule/Skill 首轮 `workspace_prepare`。Cursor MCP 开关按项目/仓库启用一次，不是每会话。Codex 新会话与真实压缩恢复都读回同一任务和 next step。

**退出条件：** 已满足。Codex 与 Cursor 都能在用户不重述任务、不提供 task ID 的情况下恢复同一 current task；真实压缩在 Codex 有证据，Cursor 明确未触发；配置、收据和模型实际行为已分开记录。

### Phase 15：接续首响与发布门

**状态：进行中（2026-11-01）。** 当前任务 `task_c593ac12bbe04d8d99294bd555fec567`。管理页升级 WorkUnit 已完成；2026-10-30 真实使用发现 Codex 项目分组不会重绑线程 cwd，旧会话仍运行在未配置 MemWeave 的 linked worktree，导致 MCP、Skill 和 Hook 全部静默失联。该接入可靠性问题升为当前阶段 P0，先于首响测量和发布门。P0 代码、真实 `haxsd-byok` 本机修复、全新 Codex CLI 加载验收，以及 2026-11-01 的 Codex 桌面/Cursor 新会话验收均已完成。Codex 当前新会话加载 22 工具且 `workspace_prepare state=ready`；Cursor 3.27.22 新会话加载 22 工具，`sessionStart` 4.295 秒返回有效上下文并首轮调用 `workspace_prepare`。Codex 本次只产生 `SessionStart` 收据，没有生成 `hook_sessions` 或模型可见启动块，实际恢复由常驻 MCP 完成；该差异保留为已知边界。剩余的是首响样本、探索路径收紧和发布门；不增加运行时、模式或默认高频 Hook，并在明确授权前不发布。

- [x] **P0（2026-10-30）：worktree 与项目绑定。** 同一 Git common directory 的主 checkout 和 linked worktrees 必须解析为同一 MemWeave project；worktree 只作为执行根，不产生重复项目或第二个 current。Hook 接受已登记仓库的合法 worktree；`clients doctor` 巡检缺失的项目级安装并显式警告/修复；新会话必须能直接看到 `memweave_*` 工具和 `workspace_prepare state=ready`。
  - [x] 同一仓库 worktree 解析、执行根/登记根返回、单项目/单 current 保持。
  - [x] Hook 接受合法 worktree，跨仓库和嵌套独立项目仍不注入。
  - [x] `clients doctor` 巡检 `worktree_unconfigured` 并显式修复，真实 worktree 已安装和复检。
  - [x] 修复 Cursor 项目级安装缺少 `.cursor/.gitignore` 导致用户仓库污染的问题。
  - [x] 从真实 worktree 新建受控 Codex CLI 会话，确认 22 工具、`workspace_prepare state=ready` 和自动 SessionStart/SessionEnd 收据；旧进程热更新不算。
  - [x] 在 Codex 桌面/Cursor 客户端的新会话中确认宿主重载、项目信任与 Hook 信任；受控 CLI 不替代桌面验收。2026-11-01 验证：Codex 新会话 `01a19499-f285-7b52-ba8a-9d7bee842bf5` 产生自动 `SessionStart` 收据、加载 22 工具并返回 `workspace_prepare state=ready`；Cursor 3.27.22 新会话 `2e5b62f6-5911-4ba9-bee5-f8f1329fe893` 的 `sessionStart` 以 4.295 秒 exit 0 返回有效上下文，MCP 加载 22 工具并在首轮调用 `workspace_prepare`。Codex 本次启动上下文没有模型可见落点，不能把收据写成注入；实际恢复路径是常驻 MCP。本轮两个新会话分别位于 MemWeave 主 checkout 和 `github-radar`，worktree 路径由受控 CLI 与 `clients doctor` 覆盖。
- [ ] 收紧首轮状态/续接提示的探索路径：优先使用 `system_status`、`workspace_prepare` 与检查点摘要；只有当前请求需要涉及实现时才展开 PLAN、全仓搜索或大范围代码阅读。
- [ ] 用至少 3 个真实 Cursor/Codex 首轮样本记录“提示发出到第一份有效状态/下一步”的耗时；Hook 启动耗时与 Agent 探索耗时分开统计，没有数据不宣称优化。
- [ ] 保持 Cursor `sessionStart` 为 best effort，并持续验证常驻 MCP + Rule/Skill 恢复；除非平台提供明确的模型交付信号，不新增逐提示或逐工具 Hook。
- [ ] 发布门：许可证、来源声明、wheel、全量回归、clients doctor、敏感信息与 Git 状态全部核对；提交、推送、标签和 Release 只在用户明确授权后执行。

## 风险与止损

| 风险 | 影响 | 预防与止损 |
|---|---|---|
| AGPL 义务遗漏 | 无法合法发布或被迫返工 | Phase 0 先落许可证、来源和修改记录；每个上游代码路径记录归属 |
| Basic Memory 上游变化 | fork 难以同步 | 固定基线；把 MemWeave 任务扩展隔离在少量模块；定期记录上游 diff |
| 数据迁移丢失 | 用户历史任务或知识不可恢复 | 迁移前备份 Markdown；先 dry-run；保留只读导出；不以 SQLite 为唯一来源 |
| 两个系统拼接复发 | 工具、索引和上下文再次分叉 | 以“单 MCP/单上下文/单存储”作为阶段门禁，未达标不进入下一阶段 |
| Hook 机制不可控 | 恢复候选误用或压缩前未落盘 | 持续写检查点；恢复后核对用户目标与代码；PreCompact 不宣称保存成功 |
| 上下文预算失控 | 任务状态被长期知识挤掉 | 统一 builder；任务优先；测量实际来源和截断 |
| 范围再次膨胀 | 又变成工具平台或图谱项目 | 每个新功能必须映射到四个场景和当前 Phase；否则不进入计划 |
| 记录隐私与污染 | 会话、命令或项目内容被误存 | 不保存 transcript、凭据和完整对话；Hook 原话只在进程内使用 |

## 非目标

- 不做独立客户端或独立聊天产品。
- 不保留两个 MCP 服务作为最终形态。
- 不支持同项目并行推进多个活跃任务。
- 不把文件/架构图谱做成独立第三层或独立引擎。
- 不重写 Basic Memory 已经成熟的语义/混合检索；许可证允许时直接融合。
- 不把管理页、安装器、工具数量或演示数据当成核心价值。
- 不在没有真实场景证据时宣称减少重复解释或提升模型质量。
- 不为每轮实现新增 plan/report；本文件是唯一计划入口。

## 防止再次跑偏

1. **先改计划，再改范围。** 任何超出当前 Phase 的功能，先更新本计划并说明对应的用户场景。
2. **一次只推进一个当前任务。** MemWeave 自己开发项目也遵循产品规则，不用多个活跃任务并行掩盖方向。
3. **每个阶段必须有可观察验收。** 配置生成、测试通过和安装成功不能代替场景验收。
4. **删除重于新增。** 融合阶段优先删除重复引擎、重复工具和重复文档，不以增加功能解决结构问题。
5. **现状与目标分开写。** 0.2.5 的行为必须标注为融合前基线；未实现能力不得使用现在时。
6. **许可证先于复制。** 每次引入上游代码先确认许可证、来源和声明，再落文件。
7. **阶段结束写检查点与状态。** 计划、`context/CURRENT_STATE.md` 和实际代码必须相互一致。

## 决策记录

### 2026-10-30：worktree 属于同一项目执行上下文

- Codex 的项目分组只是侧栏归属，不会重写已存在线程的 `cwd`、MCP 连接或工具目录；把旧会话拖进新项目也不能替代 worktree 绑定。
- 同一个 Git common directory 下的主 checkout 与 linked worktree 属于同一 MemWeave `project_id` 和同一顺序任务队列；worktree 不复用为第二个项目。
- worktree 可以有自己的项目级 `.codex` / Skill 安装，以便宿主从该目录加载 MCP/Hook；安装清单里的 `root` 可不同，但登记的项目必须通过 common directory 解析到主项目。
- Hook 的 root guard 必须接受同一仓库的 linked worktree；跨仓库/子模块仍保持静默。`clients doctor` 必须发现未配置的 linked worktree，显式报告而非静默工作，`--fix` 才写入受管配置并保留备份。

### 2026-10-27：转向 AGPL 深度融合

- 用户确认完全开源，接受以 AGPL-3.0-or-later 为基础。
- Basic Memory 从行为参考升级为直接 fork/融合的底层。
- planning-with-files 从生命周期参考升级为直接移植的 MIT 任务层。
- 最终形态是一个 MCP、一个 Skill、一个 Hook 入口，不允许两个成熟项目简单并排。
- 产品语义收敛为任务工作记忆与长期知识两层；图谱只做支撑。
- 同项目任务改为单活跃、顺序队列。
- 本轮只更新方向文档和建立计划，不宣称融合代码或真实客户端验收完成。


