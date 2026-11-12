# 手动安装

当前源码入口为 https://github.com/haxsd/memweave。产品无需模型 API Key。以下命令在可信源码克隆根目录执行，示例为 PowerShell；非 Windows 使用 `.venv/bin/python`。

下面不同的安装路径只负责把宿主接到同一个 MemWeave 服务。22 工具、任务/知识语义、项目身份和 `workspace_prepare` 契约对所有 stdio MCP 客户端相同。完整接入还需要 Skill/Rule 行为契约和启动通道；Hook 只是启动通道的一种实现，always-on Rule 或等价宿主指令也可以。Skill 通常按需加载，不能单独保证启动；只完成 MCP 配置时只能算“核心已连接”，不能算完整体验。没有 Hook 的客户端不是精简版。

## 1. 安装 Python 包

使用已核实的 Python 3.12+：

```powershell
& '<Python 绝对路径>' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install .
```

已有 `.venv` 时先检查并复用，不覆盖或删除。依赖安装需要可用网络。本轮实际验证平台为 Windows / Python 3.13。

## 2. 初始化共享记忆库并登记项目

```powershell
$memoryHome = Join-Path $env:USERPROFILE '.memweave'
$projectRoot = 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave init --home $memoryHome
$registration = @{root=$projectRoot; name='my-project'; operation_id='manual-register-my-project'} | ConvertTo-Json -Compress
$registration | & '.\.venv\Scripts\python.exe' -m memweave call project_register --home $memoryHome
```

所有客户端共享同一个 `$memoryHome`。项目身份是绝对文件夹；同一 Git 仓库的 linked worktree 复用主 checkout，不创建第二份项目或队列。

## 3. 选择客户端

推荐用受管 `setup` 安全合并配置。它会保留其他服务和 Hook、备份修改前的内容，并在受管资源被人工修改时停止。

### Codex

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root $projectRoot --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root $projectRoot --home $memoryHome --probe
```

安装位置：

| 内容 | 路径 |
|---|---|
| MCP | 项目 `.codex/config.toml` |
| Hooks | 项目 `.codex/hooks.json` |
| Skill | 项目 `.agents/skills/memweave-continuity/SKILL.md` |

Codex 使用 5 类生命周期事件：`SessionStart`、`PreCompact`、`UserPromptSubmit`、`Interrupt`、`SessionEnd`。项目必须由宿主信任，Hook 定义需通过 `/hooks` 审查；定义更新后重新审查。

如果手工合并而不是使用 `setup`，还必须在 `.codex/config.toml` 的现有 `[features]` 表中设置 `hooks = true`；只合并 `.codex/hooks.json` 不会启用 Hook。受管 `setup` 会自动完成这一步。

### Cursor

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client cursor --root $projectRoot --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave check --client cursor --root $projectRoot --home $memoryHome --probe
```

安装位置：

| 内容 | 路径 |
|---|---|
| MCP | 项目 `.cursor/mcp.json` |
| Hooks | 项目 `.cursor/hooks.json`；仅 `sessionStart` / `sessionEnd` |
| Skill + Rule | 项目 `.cursor/skills/memweave-continuity/` 与 `.cursor/rules/memweave.mdc` |

Cursor 的 `sessionStart` 是 fire-and-forget。启动块缺失不代表 MCP 不可用，首个项目请求仍应调用 `workspace_prepare`。`sessionEnd` 尚未形成完整桌面验收样本。

### Devin Desktop

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client devin --root $projectRoot --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave check --client devin --root $projectRoot --home $memoryHome --probe
```

安装位置：

| 内容 | 路径 |
|---|---|
| 用户级 MCP | `%APPDATA%\Devin\mcp_config.json` |
| Skill | 项目 `.devin/skills/memweave-continuity/SKILL.md` |
| Rule + 清单 | 项目 `.devin/rules/memweave.md` 与 `.devin/memweave.json` |

`setup` 只合并 `mcpServers.memweave`，不安装生命周期 Hook。`clients doctor --fix` 只修项目级 Skill/Rule，不写全局配置；全局 MCP 缺失必须显式运行 setup。完成后重启 Devin 并确认工具出现。

## 4. 其他 stdio MCP 客户端

先安装包并初始化记忆库，再生成标准配置：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave config --client generic --home $memoryHome
```

把输出的 `mcpServers.memweave` 合入客户端配置，并将 `src/memweave/skills/memweave-continuity/SKILL.md` 或等价指令复制到客户端支持的目录。generic 只提供 22 工具 stdio 面，不提供 setup、Hook、自动启动上下文或桌面检查。

## 5. 逐项维护

如果不用 `setup`，可以先输出配置再人工合并：

```powershell
# Cursor / Devin / generic 使用 JSON：mcpServers.memweave
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave config --client devin --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave config --client generic --home $memoryHome

# Codex 使用 TOML：[mcp_servers.memweave]
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --home $memoryHome

# Hook 输出：Codex 写入 hooks 对象；Cursor 为 version 1 的两类会话事件
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --kind hooks --root $projectRoot --home $memoryHome
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --kind hooks --root $projectRoot --home $memoryHome
```

Devin 和 generic 不支持 `--kind hooks`。人工合并必须保留已有条目、只替换 `memweave` 所有权范围，并把本机绝对路径、备份和受管资源加入目标仓库的本地忽略范围。

## 6. 验证

先运行协议检查和全机巡检：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave check --client devin --root $projectRoot --home $memoryHome --probe
& '.\.venv\Scripts\python.exe' -m memweave clients doctor --home $memoryHome
```

`check --probe` 验证配置、SDK stdio、工具面和重启读取，不代表桌面客户端已经加载。`clients doctor` 默认只读；确认项目级可修复项后才加 `--fix`。它不会改写全局配置，也不会覆盖人工修改的受管资源。

在真实客户端中重载服务，然后按工具名的完整后缀识别并读取 `memweave_system_status`，再调用 `memweave_workspace_prepare(root=$projectRoot, intent="inspect")`。Cursor 常见名称会带 `project-0-<project>-memweave-` 前缀；不要因为裸名未出现就误判为未加载。确认版本、记忆库、项目身份和 22 工具；不要为安装验收制造业务任务。

## 7. 更新

更新源码至指定版本，在同一 `.venv` 重新 `pip install .`，再执行同样的 `setup` 与 `check --probe`。保留记忆库；受管资源发生变化时审查差异。Codex Hook 定义更新后需重新信任。
