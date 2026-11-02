# MemWeave 项目接续

本目录是 MemWeave（织忆）的开发工作区。

- 状态、接续或“做到哪一步/下一步做什么”类请求是只读请求：首个项目动作必须是常驻 MCP 的 `memweave_workspace_prepare`（绝对执行根，明确 `inspect`/`resume`/`new`），可并行调用 `memweave_system_status`，再读取有界任务与检查点摘要。不得先用 shell 读取 `SKILL.md`、`PLAN.md`、`context/CURRENT_STATE.md`、README/docs 或搜索源码，也不得用 CLI/Python 调 `workspace_prepare` 绕过 MCP 硬门；不得创建、编辑或删除文件。MCP 不可用时直接说明并请求启用，不得用文件或 CLI 猜测任务状态。
- 实施类请求在完成上述接续后，再读 `README.md`、`docs/README.md`、`PLAN.md`；本机开发区存在 `context/CURRENT_STATE.md` 时一并读取，再按任务读取 `docs/` 的接口与开发说明。不要用 shell 重读已加载的 Skill。
- 前期讨论、两份 v0.1 原文、合并后的开发历程和图片位于 `docs/history/`，仅在需要追溯时读取。历史记录是参考资料，较早的建议可能已被后续决策取代。
- 当前已确认方向：AGPL-3.0-or-later 完全开源；产品语义只有任务工作记忆与长期知识两层，文件/架构关系是支撑证据；以 Basic Memory 为底层，直接深度融合 planning-with-files 的任务生命周期；最终只有一个 MCP、一个 Skill、一个 Hook 入口；同一项目单活跃任务、其余顺序排队。
- 新的重要技术选型与用户讨论后确定；常规实施细节根据已有决策推进。
- 区分已确认需求、工程建议和实际验证结果。方案中的接口、目录与客户端能力不能当作已实现。
- 后续代码和文档在本目录维护。出现持续性的项目决策、阶段进展或重要验证结果时，更新 `context/CURRENT_STATE.md`。
- 唯一长期执行计划是根目录 `PLAN.md`。任何超出当前阶段的范围调整先更新计划并说明对应的真实场景；不要为每轮实现新增独立 plan/report，过程记录合并进 `docs/history/`。
- 当前文档按职责维护：README 讲产品，`docs/specs/` 区分当前行为与融合目标，`docs/api.md` 讲接口，`docs/integrations.md` 讲接入，`docs/design-references.md` 讲参考、直接复制与许可证。单运行时、单 current 队列、并行 WorkUnit、阶段边界和 planning-with-files 生命周期适配已经落地；统一上下文、完整 Hook/桌面验收和公开发布仍不能写成已完成。
- 当前 Codex/Cursor 共用的 active 记忆库是 `%USERPROFILE%\.memweave`，已由项目安装清单和客户端配置引用，不得当缓存删除。`work/evidence/` 只保留当前版本的最小证据；`work/runtime/memory home/` 已退役，确认新库可用后可以清理。旧虚拟环境、插件缓存、一次性脚本和重复日志也可以安全清理。
- 同一 Git common directory 的 linked worktree 属于已登记项目，只能作为执行根，不得产生第二个 project 或 current；不同 clone 仍分别登记。Codex 侧栏的项目分组不会重绑已有线程的 `cwd`，worktree 缺少自己的 `.codex`/`.cursor` 受管入口、Skill 或 Hook 时必须用 `clients doctor` 发现并显式修复。
- Cursor 接入必须以目标项目作为唯一工作区根目录，用 **File → Open Folder** 打开；不要用 **Add Folders** 把它并入 `work/evidence/` 下的测试夹具或其他多根工作区。当前 Hook 对多根工作区保持沉默，Cursor 也可能只读取第一个根的 `.cursor/hooks.json`。
- Cursor 的项目级 MCP 配置存在不等于当前 Agent 会话已加载服务；必须在会话内启用/连接 MCP 并确认 `memweave_*` 工具出现。空的可选 Hook 标识和 Cursor 的 `/D:/...` 工作区路径必须按兼容输入处理，不能阻断启动上下文。
- Hook 只处理会话开始、结束和中断清理等低频生命周期；不注册逐轮 Stop follow-up，文件编辑、工具调用和逐轮原话检索走常驻 MCP。不得重新引入 `Stop`、`PostToolUse`、`afterMCPExecution`、`afterFileEdit`、`UserPromptSubmit` 或 `PreCompact` 的默认注册。
- 客户端配置或安装器发生变化后运行 `memweave clients doctor`，检查活动库登记项目、Codex 已知项目及其 linked worktree 的旧 home、重复入口、Hook 与受管资源；`--fix` 只修项目级受管安装且必须保留备份，全局配置与人工修改不得静默覆盖。
- `.venv/`、`.git/`、`.codex/`、`.agents/` 和用户目标仓库的未提交改动不属于可清理垃圾。删除前仍要核对精确绝对路径。
