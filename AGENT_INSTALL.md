# 给 Agent 的安装入口

> Cursor 的 MCP 开关按项目/仓库启用一次；`hook_sessions.startup_context_emitted=true` 仅证明 Hook 输出了上下文，不证明模型收到。Cursor `sessionStart` 是 fire-and-forget；工具可用但启动块缺失时，首轮调用 `workspace_prepare`，不要让用户重述任务。

> 状态：本入口描述 **当前安装**。主 MCP 使用 Basic Memory 单索引和统一记忆库 `%USERPROFILE%\.memweave`；顶层任务为单 current 顺序队列，任务内 WorkUnit 可并行，阶段可独立收束。Phase 12 将 Hook 收敛为低频会话生命周期：Cursor 只注册 `sessionStart/sessionEnd`，Codex 只注册 `SessionStart/Interrupt/SessionEnd`；文件编辑、工具调用、逐轮原话召回和每轮 Stop 维护改由常驻 MCP 完成。旧的 `Stop`、`afterFileEdit`、`afterMCPExecution`、`PostToolUse`、`UserPromptSubmit`、`PreCompact` 会由 setup 清理。Phase 13 已完成单一通用 MCP 工具面，共 22 个工具；诊断、恢复和管理动作走 CLI/UI，不提供 `core/full` 选择。MCP 代理写入知识默认是 candidate，project/task scope 不跨项目，user scope 需显式 opt-in。Codex CLI 与桌面应用已产生成功 `SessionStart`/`SessionEnd` 收据；Cursor 原生 Agent 已产生 `sessionStart` 输出，并在普通首轮与检查点后新会话中通过常驻 MCP 恢复同一任务，但当前 3.27.22 没有让模型收到启动块；Cursor `sessionEnd`、完整自动结束/压缩和统计收益不得写成已验证；`preCompact` 不在默认安装。计划见 [PLAN.md](PLAN.md)。

仓库：https://github.com/haxsd/memweave 。AGPL 许可证与第三方声明已在 Phase 0 落地；Phase 6 的发布准备已完成，0.2.5 开发快照已提交并推送到私有仓库，但公开标签和 GitHub Release 尚未执行。不要要求用户把访问令牌发到聊天中。

用户把仓库链接交给你并要求安装时，请实际完成以下流程。用户只是询问产品时，不要自动安装。

## 确认目标并准备环境

1. 从用户要求和当前工作区确认客户端 `codex` 或 `cursor`、当前绝对执行根，以及需要共享的记忆库。同一 Git common directory 的 linked worktree 复用已登记项目，不应登记成第二个项目；不同 clone 仍分别接入。检查目标项目规则、现有配置和 Git 状态。不要将多个相关目录擅自拆成多个项目；不确定根目录时只追问这个必要信息。
2. 复用用户已有 Python 3.12+。如有本机工具发现能力，先使用它；否则检查已配置解释器。确实缺少 Python 时，按用户的平台和安装权限准备环境，必要的人机步骤交给用户。不要重装已有环境或修改全局 PATH。
3. 通过用户现有的 GitHub 登录克隆本仓库到稳定目录，或复用已有克隆。先读取本文件及 `scripts/bootstrap.py`，不要直接执行远程下载的脚本。不要将源码放在临时目录，客户端会长期使用其中的 `.venv`。

## 实际安装

从 MemWeave 克隆目录执行，替换解释器和目标目录：

```powershell
& '<Python 3.12+绝对路径>' scripts/bootstrap.py --client codex --root 'D:\my-project' --json
```

Cursor 使用 `--client cursor`。需要跨工具共享时，对两个客户端传入同一个绝对 `--home`；省略时首次使用 `~/.memweave`，后续沿用项目安装清单。`--dry-run --json` 可以先查看完整命令计划而不写入。

引导脚本在本克隆的 `.venv` 安装依赖和产品，执行 `setup` 与 `check --probe`。它不创建业务任务、不扫描项目、不配置模型密钥、不打开 IDE 窗口。已有环境不完整、链接逃逸、配置冲突或网络错误时应根据实际错误修复后继续；不要删除已有环境、覆盖自定义配置、关闭证书校验或无限重试。网络需要代理时沿用用户已有的网络配置，不写进产品配置。

检查 JSON 中每个步骤的退出码与结果。仅配置成功不能报告协议或桌面加载成功。

安装完成后运行全机巡检，确认新入口没有与已有项目或全局配置漂移：

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave clients doctor --home '<共享记忆库绝对路径>'
```

默认只读并会从已登记仓库和 Codex 已知项目发现 linked worktree；项目级可修复项确认后可加 `--fix`。巡检会把受管 Skill/Rule 与当前包内资源比较，源码升级后的过期资源也属于可修复漂移。worktree 缺少项目级入口时报告 `worktree_unconfigured`，Cursor 本机受管入口缺少忽略规则时报告 `local_ignore_missing`。全局入口和人工修改的受管资源只报告，不应由安装 Agent 擅自覆盖。巡检发现 `repaired` 后仍需让用户重载对应客户端，并重新检查工具与 Hook 实际行为。

## 宿主必须参与的步骤

- Codex：项目 `.codex/` 需要被宿主信任；非托管 Hooks 需要用户在支持此功能的客户端审查**当前定义**并信任。官方 CLI 可使用 `/hooks`。定义更新后需要重新审查。侧栏项目分组不会重绑旧线程的 `cwd`；worktree 必须从自己的执行根加载配置。不要代写信任数据库，也不要把跳过信任审查的启动参数放进安装配置。
- Cursor：在用户已经打开的窗口中重新加载 MemWeave MCP；如果全局和项目中已有同名服务，先检查实际生效来源，保留其他服务。Cursor `3.27.14` 中同名全局服务会遮蔽项目级配置；两者应指向同一记忆库，或只保留一个同名项。不要自动再开窗口。
- 如客户端要求确认服务或执行命令，说明要批准的具体配置，让用户在现有客户端完成。

只在这些必要的人机步骤停下，交付具体配置路径和检查结果。之后读取 `memweave_system_status`，对目标根目录调用 `memweave_workspace_prepare(intent="inspect")`，核对版本、记忆库和工具数。使用一次真实项目任务验证保存、恢复和提炼；不为安装验收制造业务任务。

## 更新和检查

在同一源码克隆更新到用户选择的版本，再重新执行 bootstrap；它重新安装包并同步受管资源。不要自动切换分支、丢弃本地修改或升级到未指定的新版本。

日常只检查：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root 'D:\my-project' --probe
& '.\.venv\Scripts\python.exe' -m memweave clients doctor
```

`setup`/`check` 默认客户端仍为 Cursor，Codex 必须显式选择。自定义 Skill 或 Hook 被修改时保留原内容，展示冲突并合并，不以“更新”为由覆盖。

手动逐项配置见 [手动安装](docs/manual-install.md)。产品使用见 [Agent 指引](docs/agent-guide.md)，事件与信任边界见 [接入说明](docs/integrations.md)。
