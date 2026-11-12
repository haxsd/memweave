# 客户端接入与验证

> 所有 stdio MCP 客户端共享同一个 22 工具核心、任务/知识模型、绝对文件夹项目身份和 `workspace_prepare` 恢复契约。完整接入还需要 Skill/Rule 行为契约和可工作的启动通道；Hook 只是启动通道的一种实现。

## 统一核心与宿主适配层

| 层 | 是否统一 | 说明 |
|---|---|---|
| MCP 核心 | 统一 | 同一 22 工具、任务/知识模型、项目身份和安全写入 |
| 行为契约 | 内容一致，格式随宿主 | Skill 规定工作循环；always-on Rule/首轮宿主指令负责激活 |
| 启动通道 | 完整接入必需，实现不同 | Hook、always-on Rule 或等价首轮指令 |
| 压缩恢复 | 实现不同 | 有压缩快照的宿主可自动恢复；没有的宿主重新 `workspace_prepare` 并核对实际文件 |
| 验证证据 | 因宿主而异 | 配置、协议自检、真实模型回合和收益样本必须分开记录 |

**只看 `memweave_*` 工具是否出现不足以判断完整接入。** 没有启动通道时，只能标记“核心已连接，尚未完整接入”，不能宣称完整体验。

## 通用 stdio MCP 客户端

先安装包并初始化共享记忆库，再输出标准配置：

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m pip install .
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave init --home '<共享记忆库绝对路径>'
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave config --client generic --home '<共享记忆库绝对路径>'
```

把输出中的 `mcpServers.memweave` 合入客户端配置，并把 `src/memweave/skills/memweave-continuity/SKILL.md` 复制到客户端支持的技能目录。还必须安装一条 always-on Rule 或等价首轮宿主指令，要求首个项目动作调用 `workspace_prepare`。

通用路径不附带自动 setup、Hook 或桌面验收，但连接的是同一个 22 工具核心；缺少启动通道时不算完整接入。

## 受管安装

Codex、Cursor、Devin Desktop 支持受管 setup/check。首次接入建议交给 Agent，按 [AGENT_INSTALL.md](../AGENT_INSTALL.md) 先盘点环境和客户端，再执行 bootstrap。

```powershell
& '<Python 3.12+ 绝对路径>' scripts/bootstrap.py --client cursor --root 'D:\my-project' --dry-run --json
```

确认计划后去掉 `--dry-run`。需要跨客户端共享时显式传入同一个绝对 `--home`。

### Codex

- 项目配置：`.codex/config.toml`、`.codex/hooks.json`、`.agents/skills/memweave-continuity/`。
- 启动通道：生命周期 Hook；项目必须由宿主信任，Hook 定义变化后重新审查。
- 压缩恢复：`PreCompact` 保存有界快照，`UserPromptSubmit` 只消费待恢复快照；不做逐轮召回。
- 侧栏分组不会重绑旧线程的 `cwd`；worktree 从自己的执行根加载配置。

### Cursor

- 项目资源：`.cursor/mcp.json`、`.cursor/hooks.json`、`.cursor/rules/memweave.mdc`、`.cursor/skills/`。
- 启动通道：always-on Rule 是主通道；`sessionStart` 是 best-effort，不能替代 Rule。
- 只支持单项目根目录；请用 **File → Open Folder** 打开目标项目，不要用 **Add Folders** 合并多个根。
- 项目级 MCP 配置存在不等于当前会话已加载；需要在会话内启用/连接并确认 `memweave_*` 工具出现。
- 没有 MemWeave 压缩 Hook；长上下文后的恢复依赖首轮回退和主动检查点。

### Devin Desktop

- 全局配置：Windows 默认 `%APPDATA%\Devin\mcp_config.json`。
- 项目资源：`.devin/skills/`、`.devin/rules/` 和安装清单。
- 启动通道：always-on Rule；首版不安装 Hook，没有自动上下文注入或压缩快照。
- `clients doctor --fix` 只补项目级资源，不写全局配置；全局缺失必须显式 `setup --client devin`。

## 检查与修复

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave check --client devin --root 'D:\my-project' --probe
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave clients doctor
```

`check --probe` 验证配置、SDK stdio、工具面和重启读取，不代表桌面客户端已经加载。`clients doctor` 默认只读，按“已发现项目根 × 选定客户端”报告完整覆盖；确认项目级缺口后才加 `--fix`，它会保留备份，不静默覆盖全局配置或人工修改的受管资源。

接入后必须让用户重载客户端。部分宿主会给工具名加服务前缀，例如 `project-0-<project>-memweave-memweave_*`；应按完整名称的后缀识别，不要把裸名未出现误判为未加载。

## 验证边界

- **代码行为**：自动化测试和协议探针可以证明。
- **配置已写入**：安装器可以证明。
- **桌面已加载**：需要客户端实际重载和工具列表确认。
- **模型已遵从**：需要真实模型回合，不能由配置、收据或探针推断。
- **收益**：需要对照实验，不由单次成功示例推断。

`desktop_verification` 只能由真实桌面观察更新；未观察的客户端行为保持未验证。

## 不需要的 Hook

默认不注册 `Stop`、`PostToolUse`、`afterMCPExecution`、`afterFileEdit` 或逐轮原话检索。任务读取、检查点、WorkUnit、收尾和知识审查由常驻 MCP + Skill 完成；Hook 只在会话开始、压缩/中断、结束等边界参与。
