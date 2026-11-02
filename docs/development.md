# 本地开发与验证

> 状态：本文描述 **当前开发流程**。Basic Memory 已经作为单一底层接入，活动库位于 `%USERPROFILE%\.memweave`；顶层任务使用单 current 顺序队列，WorkUnit 支持并行子代理，planning-with-files 的计划/发现/失败/验证纪律已进入统一任务与 Skill。Phase 3 已统一检索和上下文来源；Phase 4 验证单一 Hook/Skill 事件契约；Phase 6–8 完成真实场景、跨客户端接续、完成门禁和任务契约；Phase 9 实现全机客户端巡检和真实桌面工作记忆生命周期；Phase 10 加入架构记录 update/forget 和 knowledge/architecture 状态计数；Phase 11 把项目状态与架构维护接入管理页；Phase 12 完成有界历史、独立检查点、归档和低频 Hook；Phase 14 验证真实新会话和压缩恢复。Cursor `sessionStart` 只按 best effort 处理，3.27.22 实测启动块未进入模型上下文，实际恢复必须依赖首轮 MCP。`preCompact`（未安装）、Cursor `sessionEnd`、完整桌面自证和统计收益仍待验收。见 [PLAN.md](../PLAN.md)。

## 当前工程范围

源码版本仍为 0.2.5。主 MCP、存储生命周期和知识检索接到 Basic Memory：一个进程只创建一个 `Service`、一个 Basic Memory index 和一个 SQLite FTS5 后端；旧的 `records` 表与本地词法搜索已删除。顶层任务改为单 `current` 与确定 `queued` 顺序；任务内 WorkUnit 独立 revision/etag，可并行运行子代理；阶段记录可独立收束。Hook 只做低频任务恢复；当前请求的原话召回、任务绑定和检查点维护由常驻 MCP 完成。Codex 原生 CLI 已完成三个真实模型场景；Cursor `3.27.14` 桌面已完成一次真实 MCP 跨客户端接续；`3.27.22` 的 `D:\project` 有 `sessionStart` 进程输出、MCP 调用和旧版 `stop` 证据，但 Phase 14 的模型可见验证表明启动块未送达。WorkUnit 只隔离记录，不隔离工作树，并行文件编辑仍需独立 worktree 或互斥文件范围。

已有工程能力包括单 current 队列、并行 WorkUnit、阶段边界、任务检查点、按范围保存的知识、候选与显式启用、Basic Memory FTS 检索、纠正与遗忘、文件结构和有来源的架构图谱，以及版本、内容指纹、跨进程锁与中断恢复。浏览器管理页保留为可选辅助，支持项目级状态和架构纠正/遗忘；探针和 Hook 收据仅作诊断，Hook 返回的上下文与跟进仍须宿主实际使用。

运行报告保存在本机开发区，不随源码分发。Codex 与 Cursor 安装入口以当前源码和实际测试为准；Codex CLI 已有真实模型证据和单轮收益对照，当前 Codex 桌面回合已观察 Hook 注入；Cursor 已有一次真实桌面跨客户端接续和当前 `stop` 收据，但新会话注入、部分事件和更长会话持续体验仍未验收。参见 [产品规格](specs/2026-10-27-product-realignment.md)。

## 环境

Python 要求 `>=3.12`，本轮实测 Windows x64 / Python 3.13.15。项目使用独立 `.venv`，不会安装到全局 Python。MCP 使用官方 SDK `2.3.0`，按该版本的 `MCPServer` 与 `Client` API 实现。

在 Windows / Python 3.13 环境中：

```powershell
& '<已核实的 Python 绝对路径>' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.windows-py313.txt
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e .
```

如果从当前 Codex 项目继续开发，先通过 toolkit-map 核对 Python 路径。锁定文件只对应本轮 Windows / Python 3.13 组合；其他系统或 Python 版本需重新解析并验证依赖，不能把该锁定文件当作跨平台锁。

## 接入与主动调用

从项目根目录执行，两个客户端保持同一个 `--home`：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave init --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --home "$HOME\.memweave"
```

配置输出与包内原创 Skill 的手动复制方式见 [接入说明](integrations.md)，宿主内四时点调用顺序见 [Agent 指引](agent-guide.md)。没有经过真实事件验证的自动化时，主动使用 MCP/Skill 流程。

只读 `workspace_prepare(root, intent="inspect", task_id=None, query="", include_user=False, budget=12000)` 供 MCP 与 CLI 共用。根目录必须是存在的绝对执行根；子目录和不同 clone 不自动归并，同一 Git common directory 的 linked worktree 会复用已登记项目。不隐式注册、创建任务或扫描。`inspect/new` 不选任务，`resume` 只恢复唯一 current；显式 task_id 也必须是 current。queued 任务只按队列等待，未注册返回 `registration_required`。

CLI 使用 `call` 的 `workspace_prepare` action，参数为同一 JSON 对象。Windows 路径与 JSON 输入方式见 [API 说明](api.md)，不要依赖客户端的启动工作目录来推断目标根目录。

返回 `state`、`project`、`tasks`、`pending_consolidation`、`pending_phases`、`work_units`、`attention`、`completion` 与 `context`；各简表有上限及截断提示。上下文组合受总字符预算约束，纳入 current 任务、运行中/阻塞 WorkUnit、已启用知识和有效局部图谱，给出 `included_sources`、截断与图谱覆盖/新鲜度。`context.reboot` 是五问摘要；`attention` 和 `completion` 是机器可读的恢复提示，不代替 `task_close` 的逐项验收。用户范围知识必须通过 `include_user` 显式选择，候选与来源漂移架构不会作为有效内容纳入。

## 诊断、演示与可选页面

按需执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave doctor --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave clients doctor
& '.\.venv\Scripts\python.exe' -m memweave probe --home "$HOME\.memweave"
& '.\.venv\Scripts\python.exe' -m memweave demo --home "$HOME\.memweave" --root .
& '.\.venv\Scripts\python.exe' -m memweave ui --home "$HOME\.memweave"
```

`init` 创建配置、操作状态库、派生 Basic Memory index 与运行目录，旧数据库自动升级到 schema 2。再次调用保留现有记录；遇到未知配置或数据库版本时报错。`doctor` 不初始化缺失的目录。配置 schema 仍为 1。

`clients doctor` 是跨项目只读巡检：默认合并活动库项目登记与 Codex 已知项目，逐目标检查全局/项目入口、旧 home、解释器、MCP、Hooks、受管资源和项目登记。`--root` 可重复补充目录；`--fix` 只对项目级可修复项复用 setup 并保留备份，全局配置和人工改动不会被静默覆盖。修复后必须重载客户端；命令不会向 MCP 暴露新工具。

`probe` 启动第一个真实 stdio 子进程，完成握手、列工具和标记写入，随后关闭它；启动第二个进程读取同一个标记。每次生成唯一 `operation_id`，保留该条诊断记录。成功结果的 `verification_source` 为 `sdk_stdio`，不表示任何桌面客户端已经加载。

`serve` 需要已初始化的库，启动时迁移并恢复中断操作，然后由同一个 `Service` 启动唯一的 Basic Memory index；通常由客户端按生成的配置启动，标准输出只用于 MCP 协议。`call` 也使用同一生命周期，参数与冲突恢复见 [API 说明](api.md)。

`demo` 是两个独立 MCP 进程的工程演示，会恢复并完成两个并行 WorkUnit、收束阶段、执行检查点恢复、知识检索与纠正，并留下带演示标记的数据；不代表真实桌面 Agent 已适时记录或主动使用知识。`ui` 可以初始化空库，但页面不是日常工作或接入 MCP 的前置条件。

### 本地管理页

`src/memweave/static/index.html` 是可选辅助界面，使用原生 HTML/CSS/JavaScript，无 CDN、字体下载或第三方运行依赖。页面支持项目选择与注册、任务检查点/结束、知识查看/纠正/遗忘、文件结构与架构浏览，以及架构纠正/遗忘。总览同时显示当前项目和全局的 knowledge scope/status 与 architecture stale 计数；图谱请求包含 stale 架构，节点详情显示 revision、etag、来源、关系和过期原因。

HTTP 只绑定 `127.0.0.1`。启动时从 URL 接收一次性令牌，页面随即清理地址栏；令牌只放内存和 `sessionStorage`。`GET /api/status` 返回全局状态，`POST /api/status` 可带 `project_id` 返回项目视图，其余均为带 Bearer 头的 POST；校验 Host、Origin 和跨站标识，不提供 CORS。写入使用 `crypto.randomUUID()` 生成操作标识，更新/结束/遗忘携带 revision 与 etag，冲突时要求重新读取。来源漂移的架构必须由用户显式确认后重新扫描并读取当前哈希，不能静默接收变化。请求内容、令牌和正文不写访问日志。

页面验证覆盖过项目注册、任务创建/检查点/重载恢复/结束、知识生命周期、扫描与图谱、版本冲突提示，以及 1440px 和 390px 宽度。它仍是本机单用户辅助入口，不是移动端或公网服务。

## 测试与构建

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.test_services tests.test_context -v
& '.\.venv\Scripts\python.exe' -m unittest tests.test_mcp -v
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv\Scripts\python.exe' -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

测试使用临时目录与真实 SQLite、Python 子进程、HTTP 与 MCP SDK；覆盖中文、空格、单引号路径、幂等、并发写入、真实进程突然退出后的恢复、外部编辑冲突、遗忘防复活、范围隔离、中文检索、来源失效、Windows junction、HTTP 本机边界、stdio 重启和 legacy 协议。符号链接测试可能因 Windows 权限跳过，junction 有独立真实测试。

测试中的故意冲突、找不到探针和参数越界会由 SDK 在 stderr 输出预期错误；以 unittest 结果与进程退出码判断通过。Windows 测试还实际执行生成的 PowerShell Hook 命令。Phase 1 新增测试会删除派生 Basic Memory index，确认只从 Markdown 重建同一搜索结果，并验证单 MCP stdio 进程重启后任务、知识和探针仍可读取。Phase 2 的在线重建测试在另一个 SQLite 连接保持打开时清空并重建派生表；两个真实 stdio 服务进程并行更新不同 WorkUnit 时，父任务 revision 不变。Phase 3 新增无兜底召回、任务优先预算、项目隔离、重建后上下文一致、默认工具面、真实 Hook+MCP 后续复用，以及跨进程读取等待锁并恢复 pending 写入的测试。Phase 4 新增 Codex→Cursor 同 home 跨客户端任务与 WorkUnit 恢复测试。Phase 5 新增阶段知识候选、激活、下一任务复用、无关查询、纠正和遗忘的完整场景测试。Phase 6 的单轮三臂对照显示 A/C 通过隐藏验收、B 失败；C 恢复了预置策略但没有效率优势。Phase 7/8 新增 reboot、attention、completion readiness、contract digest、drift 和显式 re-attest 测试。Phase 9 新增跨项目巡检、旧 home 修复、全局冲突不写、资源漂移保护、缺失根目录、CLI JSON、Cursor 空标识/盘符路径、启动上下文不重复回退、管理页范围筛选，以及核心写工具扁平参数 schema 测试。Phase 10 新增架构 update/forget、来源漂移、跨项目拒绝、tombstone 防复活和 knowledge/architecture status 汇总；当时双工具面测试已在 Phase 13 收敛为单一通用面。Phase 11 新增项目级 status 投影、Web 架构生命周期、来源漂移后显式重扫、关系/etag 展示契约和管理页真实浏览器闭环；全量 197 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 612.127 秒。Phase 12 新增工作批次归档、检查点分页、低频 Hook 与跨进程恢复；Phase 13 新增单一通用 22 工具面、旧 `--toolset` 兼容迁移和内部诊断探针。最终全量 203 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 598.362 秒；60 项定向回归通过。

Phase 14 新增 `startup_context_emitted`/`startup_context_source` 状态命名、旧 `context_delivered` 读取迁移和 required action 回归；Cursor 官方 `sessionStart` fire-and-forget 行为以模型可见上下文实测记录。Phase 14 定向 35 项、全量 204 项运行（1 项既有 Windows 符号链接权限跳过）通过，耗时 669.450 秒；`clients doctor` 复检 6/6 ready，最终项目扫描 611 个文件、0 错误。

Phase 15 P0 新增 Git common directory worktree 解析、`project_register`/`workspace_prepare` 复用主项目、Codex Hook worktree 路径接受、`clients doctor` 的 `worktree_unconfigured` 发现/修复，以及 Cursor `.cursor/.gitignore` 合并。真实 `haxsd-byok` worktree 已修复并保持 5 个项目；主 checkout 与 worktree 的 `check --probe` 均返回 22 工具。全新 Codex CLI 会话实际调用 `system_status`/`workspace_prepare` 并返回 `state=ready`，同时留下自动 SessionStart/SessionEnd 收据。2026-11-01 进一步完成真实客户端新会话验收：Codex 桌面新会话加载 22 工具、返回 `workspace_prepare=ready` 并留下自动 `SessionStart` 收据；Cursor 3.27.22 新会话加载 22 工具，`sessionStart` 以 4.295 秒 exit 0 返回有效上下文，并首轮调用 `workspace_prepare`。Codex 本次只有收据，没有 `hook_sessions` 或模型可见启动块，实际恢复路径仍是常驻 MCP。定向 32 项通过，全量 212 项运行、1 项仅因 Windows 符号链接权限跳过且其余通过，耗时 763.653 秒；`clients doctor` 最终 7 roots / 8 targets 全 ready。

2026-11-02 Phase 15 收口验证：3 个真实首轮样本为 Codex 26.0 秒、Cursor 修复前 230.4 秒、修复后 75.3 秒；修复后首个项目动作是 MCP `workspace_prepare`，不再先读 PLAN/源码或通过 CLI 绕过硬门。干净源码副本重建的 `memweave-0.2.5-py3-none-any.whl` 为 997,244 字节、SHA-256 `1DDDC80BE5031DA25C32A5F91F4E92CE00840F15328EBEE1D3A4D7B5F3040783`；独立 venv 安装、`init`、`doctor`、stdio `probe` 重启读回与 `pip check` 通过。最终全量回归 217 tests / 1 skipped / OK（649.713 秒），`clients doctor` 7 roots / 8 targets ready。旧 `build/lib` 的遗留资源没有进入干净 wheel；旧 MCP prompt/resource/UI 模块导入均为缺失。上述改动包含在本次聚焦提交中。

本轮需验收绝对执行根、同仓库 linked worktree 复用项目、单 current 约束、并发任务创建、queued 顺序推进、并行 WorkUnit、阻塞关闭保护、阶段独立收束、`new` 不恢复旧任务、准备入口无隐式写入、用户范围显式选择、长历史下知识与结构的预算分配、漂移架构排除、实际纳入的来源标识，以及真实 MCP 服务重启恢复。命令清单表示验证方法，是否通过以本轮执行记录为准。

## 代码边界

| 文件 | 职责 |
|---|---|
| `src/memweave/diagnostics.py` | 初始化、状态、SQLite 探针与事件收据 |
| `src/memweave/integrations.py` | 生成客户端配置与正确引用的启动参数 |
| `src/memweave/installation.py` | Cursor 项目安装更新、清单、备份与配置/协议检查 |
| `src/memweave/codex_installation.py` | Codex TOML 与 Hook 合并、Skill、安装清单和检查 |
| `src/memweave/client_doctor.py` | 跨项目客户端入口发现、问题汇总、项目级显式修复与全局冲突报告 |
| `src/memweave/server.py` | MCP 工具声明、参数边界与业务错误转换 |
| `src/memweave/basic_memory_engine.py` | Basic Memory 的单项目 Markdown/SQLite/搜索/上下文适配器 |
| `src/memweave/basic_memory_index.py` | 单进程事件循环、共享索引生命周期与同步/检索封装 |
| `src/memweave/probe.py` | 有界 stdio 握手和重启读取 |
| `src/memweave/cli.py` | argparse 入口、JSON 输出与 Hook 输入 |
| `src/memweave/hooks.py` | Cursor 上下文注入、会话任务关联、编辑标记与有界跟进 |
| `src/memweave/codex_hooks.py` | Codex 原生事件、压缩恢复、工作观察与有界续轮 |
| `scripts/bootstrap.py` | 克隆内准备环境、安装包、setup 与协议自检 |
| `src/memweave/storage.py` | Markdown 权威记录、锁、操作日志、墓碑与恢复；不再保存业务记录副本 |
| `src/memweave/services.py` | 项目、单 current 队列、WorkUnit、阶段、知识与上下文应用服务 |
| `src/memweave/context.py` | 当前上下文组合器：任务与 WorkUnit、总字符预算、知识与局部图谱、实际来源 |
| `src/memweave/project_map.py` | 有界扫描、文件证据、架构关系与失效 |
| `src/memweave/web.py` | 本机 HTTP 与随机令牌访问 |
| `src/memweave/static/index.html` | 可选中文管理页 |
| `src/memweave/demo.py` | 两个独立 MCP 服务进程的完整演示 |

构建本轮 wheel 后，应在独立环境安装实际构建的 wheel，离开源码目录核对 `--version`、新入口、上下文与真实 MCP 重启，再执行 `init`、`doctor`、`probe` 和包资源检查。历史 wheel 已在文档定稿后重建，并完成全新 venv 安装、`pip check`、stdio 重启读回和资源/许可证检查；更早 wheel 只作为 superseded 证据保留，不能混用其安装结果。2026-11-02 精简 Basic Memory 后，目标 wheel 只保留其 Alembic 脚本、原创 Skill 和许可证文本；旧 Wheel 中的提示模板与 MCP resources/UI 已不再属于当前发行内容，必须重建并重新核对。资源存在不能证明目标客户端已安装或触发它。可选页面没有 npm 构建或前端运行依赖。

不把 `clients.*.desktop_verification` 手动改为“已通过”来代替桌面验收。Cursor `3.27.22` 的 `D:\project` 有 `sessionStart` 进程输出、MCP 调用、可见状态行、真实任务创建和检查点历史；`github-radar` 的后续真实 Agent 会话完成过顺序任务、关闭和提炼。Phase 14 进一步区分了进程输出与模型交付：同版本的新会话模型没有收到启动块，但通过常驻 MCP 的 `workspace_prepare` 恢复了任务。Phase 12 起默认安装只保留低频会话 Hook，旧的 `afterMCPExecution`、`afterFileEdit`、`PostToolUse` 和 `UserPromptSubmit` 触发证据只作为历史。当前配置仍须在客户端重载后重新观察 Codex 的 SessionStart/Interrupt/SessionEnd 与 Cursor 的 sessionStart/sessionEnd；Codex 结束事件与 Cursor `sessionStart` 进程输出已有样本，Cursor 的模型可见启动注入、`sessionEnd` 与 `preCompact`（未安装）仍缺通过证据。单轮 Codex CLI 对照只证明一次精确知识召回，不证明统计收益，也不替代跨客户端或完整桌面 Hook 验收。历史证据见 `work/evidence/2026-10-28-phase9/`。
