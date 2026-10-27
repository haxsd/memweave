# 实现参考与取舍

本产品参考以下项目的公开接口、安装体验与生命周期设计，实现代码与 Skill 正文由本项目编写，没有导入其源码。

| 来源 | 本轮读取的实现 | 采用的思路 | MemWeave 的边界 |
|---|---|---|---|
| [planning-with-files](https://github.com/OthmanAdi/planning-with-files) | `.codex/hooks.json`、`.codex/hooks/stop.py`、Skill；master `dcdfb278de53fdb69b2067ea173dbb1de45345a3` | Hook 使用原生宿主事件，先有明确任务关联，停止时检查实际状态并防止重复续轮；恢复后重新读取工作状态 | 状态来自真实 MCP 结果与记录；保持 Markdown + SQLite，不引入其三文件计划布局，不回放会话历史 |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | `llms-install.md`、`src/basic_memory/mcp/project_context.py`；main `438d49776b8682f0a561bb3dc66092a33a5a829b` | 为 Agent 提供专门安装入口，把包安装、MCP 接入、验证和更新串起来；集中处理项目定位 | 采用本机精确根目录和显式任务意图，跨工具共用记忆库；不引入云同步或默认项目推断 |

读取时间为 2026-10-27。上表 SHA 为当时核对的分支提交；部分文件先按分支读取，未对每个参考文件做提交归属校验。planning-with-files 仓库显示 MIT，Basic Memory 显示 AGPL-3.0；此处只研究行为与接口，未将其实现改编进本项目。

对应实现：

- `AGENT_INSTALL.md` 与 `scripts/bootstrap.py`：给 Agent 的仓库入口和实际安装命令；`docs/manual-install.md` 保留用户逐项配置路径。
- `codex_installation.py`：保留现有配置、局部安装与更新、只读检查及协议自检；必须的人机信任步骤显式返回。
- `codex_hooks.py`：启动背景、明确任务绑定、修改或验证后的待记状态、一次 Stop 维护、中断保护、压缩后恢复。
- `skills/memweave-continuity/SKILL.md`：宿主在四时点执行理解与写入，Hook 只促进执行；提醒不是完成证明。

判断优化是否成立，分别观察实际配置、原生事件、MCP 写入和模型行为。当前原生 CLI 的受控验证证明链路可执行；旧版和新版 Skill 的两个同题执行样例都达标，没有测出增量收益，不能据此声称普遍减少遗漏或降低调用成本。
