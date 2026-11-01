# 文档总览

当前产品文档按职责维护，不再为每轮实现新增独立 plan/report。方向是 **AGPL-3.0-or-later 完全开源 + Basic Memory/planning-with-files 深度融合**。Phase 0–11 已完成基础运行时、单 current/并行 WorkUnit、统一 MCP/上下文、真实桌面接续、知识复用、管理页维护和架构记录生命周期。Phase 12 已完成：任务历史收敛为有界分页与归档，检查点和阶段不再追加到任务正文；Codex 和 Cursor 的 Hook 面统一为低频会话事件，逐工具/逐编辑召回改走常驻 MCP，不再提供轻量/全量双轨。Phase 13 已完成：`core/full` 收敛为单一通用 22 工具面，诊断和恢复动作转 CLI/UI。Phase 14 已完成真实恢复验证：Codex 新会话及真实压缩后保持同一任务；Cursor 在普通首轮和检查点后新会话中，通过常驻 MCP + Rule/Skill 自动恢复任务。Cursor `sessionStart` 是 fire-and-forget，当前模型未收到启动块，因此只按 best effort 记录。Phase 15 P0 已完成：真实 worktree 复用同一项目，doctor 可巡检并修复缺失入口；Codex 桌面与 Cursor 新会话均加载 22 工具，Codex 返回 `workspace_prepare=ready`，Cursor `sessionStart` 4.295 秒成功并首轮恢复。Codex 本次启动块未进入模型，实际恢复由常驻 MCP 完成。剩余首响样本、Cursor `sessionEnd`、压缩事件（`preCompact` 未安装）、统计收益和公开发布动作。

唯一长期执行计划：[PLAN.md](../PLAN.md)。任何范围变化先更新该计划。

## 先读

- [项目说明](../README.md)：产品定位、能力、安装入口和边界。
- [AGPL 深度融合实施计划](../PLAN.md)：唯一计划入口、阶段、验收、风险与防跑偏规则。
- [AGPL 深度融合目标规格](specs/2026-10-27-agpl-fusion-target.md)：两层记忆、单 MCP/Skill/Hook、顺序任务与跨客户端目标行为。
- [AGPL 许可证](../LICENSE) 与 [第三方来源声明](../THIRD_PARTY_NOTICES.md)：许可证、固定上游提交、直接复制范围与同步义务。
- [Agent 安装入口](../AGENT_INSTALL.md)：把仓库交给已有 Agent 完成环境与接入。
- [手动安装](manual-install.md)：逐项安装、配置、更新与验证。

## 使用与接口

- [Agent 使用指引](agent-guide.md)：开始/恢复、执行、完成提炼和下一次复用。
- [API 与存储](api.md)：MCP/CLI/HTTP action、记录模型、恢复协议和检索边界。
- [客户端接入](integrations.md)：Codex / Cursor 的 setup、check、Hooks 和验收区别。
- [开发说明](development.md)：开发、测试、构建、演示和本地管理页。

## 设计、现状与目标

- [实现参考与融合决策](design-references.md)：两个上游项目的直接复制/fork 范围、固定提交、许可证与来源义务。
- [产品行为基准](specs/2026-10-27-product-realignment.md)：融合前的四时点默认行为与声明边界，用于迁移和历史对照。

## 本机资料（不随源码分发）

- `context/CURRENT_STATE.md`：当前开发接续状态。
- `docs/history/`：原始讨论、两份 v0.1 原文、合并后的开发历程和附件。
- `%USERPROFILE%\.memweave`：当前 Codex/Cursor 共享的 active 记忆库，不能当缓存删除。
- `work/evidence/`：当前版本保留的最小验证证据。

当前文档必须明确区分 0.2.5 已实现行为与融合目标。历史文档只用于追溯，不能覆盖当前用户要求或把旧方案恢复为默认方向。
