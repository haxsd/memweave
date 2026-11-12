# 给 Agent 的安装入口

> 用户把仓库链接交给你并要求安装时，按本文实际完成接入；用户只是询问产品时，不要自动安装。

MemWeave 是一个本地 stdio MCP。所有客户端应指向同一个记忆库 `--home`，并且从同一个项目绝对路径恢复同一项目身份。仓库：https://github.com/haxsd/memweave

完整接入需要三层同时成立：MCP 提供能力，Skill/Rule 规定工作循环，启动通道在首轮激活它。Skill 通常按需加载，不能单独保证启动；Hook 只是启动通道的一种实现，always-on Rule 或等价首轮指令也可以。**只有 MCP 工具出现、没有启动通道时，只能标记“核心已连接，尚未完整接入”。**

## 0. 先盘点环境和客户端

先做只读检查，不要直接写配置。至少确认：

1. **系统与解释器**：操作系统、CPU 架构、Python 3.12+ 的绝对路径；现有 `memweave` 包或命令是否已安装。
2. **包管理器**：`uv`、`pipx`、`pip` 哪些可用；是否需要建独立 `.venv`。
3. **客户端**：当前实际使用哪个 MCP 客户端；是否装了多个；各自配置在哪里。
4. **目标项目**：绝对执行根、是否 Git 仓库、是否有未提交改动；不要擅自拆分、移动或提交用户项目。
5. **现有 MemWeave**：是否已有 `.cursor/`、`.codex/`、`.devin/` 或全局 MemWeave 配置；是否指向另一个记忆库。

常见位置：

| 客户端 | 需要检查 |
|---|---|
| Codex | 项目 `.codex/config.toml`、`.codex/hooks.json`、`.agents/skills/`；全局 `~/.codex/` |
| Cursor | 项目 `.cursor/mcp.json`、`.cursor/hooks.json`、`.cursor/rules/`、`.cursor/skills/` |
| Devin Desktop | 用户级 `%APPDATA%\Devin\mcp_config.json`；项目 `.devin/skills/`、`.devin/rules/` |
| 其他 stdio MCP 客户端 | 用户已有的 MCP 配置位置和 Skill/Rule 目录 |

不要要求用户把 GitHub、模型或客户端访问令牌发到聊天中，也不要关闭证书校验。

## 1. 选择安装路径

- **优先使用已有客户端受管安装**：Codex、Cursor、Devin 支持 `scripts/bootstrap.py --client ...`。
- **其他 stdio MCP 客户端**：使用 `config --client generic`，由用户或 Agent 手动合并配置，并安装 Skill 和 always-on Rule/首轮指令。
- **同一个文件夹跨客户端共享**：显式传入同一个绝对 `--home`；不要为每个客户端建独立记忆库。
- **linked worktree**：同一 Git common directory 的 worktree 复用主 checkout 的项目身份；不同 clone 分别登记。

如果目标项目已经有人工维护的 MCP、Hook、Rule 或同名 `memweave` 服务，先报告冲突，不要静默覆盖。

## 2. 先给计划，再安装

在写入前向用户说明：

- 选择的客户端和配置路径；
- 将创建或修改哪些文件；
- 使用的 Python 和记忆库绝对路径；
- 是否需要重载客户端；
- 哪些验证只能由用户在桌面完成。

然后先跑 dry-run：

```powershell
& '<Python 3.12+ 绝对路径>' scripts/bootstrap.py --client codex --root 'D:\my-project' --dry-run --json
```

把 `--client` 换成 `cursor` 或 `devin`。需要共享记忆库时显式传入：

```powershell
& '<Python 3.12+ 绝对路径>' scripts/bootstrap.py --client cursor --root 'D:\my-project' --home 'C:\Users\<user>\.memweave' --dry-run --json
```

确认计划后才执行同一条命令，去掉 `--dry-run`。

## 3. 受管安装做什么

`scripts/bootstrap.py` 会复用或创建克隆内的 `.venv`，安装当前源码，执行 `setup` 和 `check --probe`。它不创建业务任务、不扫描项目、不配置模型密钥、不打开客户端窗口。

- **Codex**：写入项目 `.codex/config.toml`、`.codex/hooks.json` 和 `.agents/skills/memweave-continuity/`。项目必须由宿主信任；Hook 定义变化后需要重新审查。
- **Cursor**：写入项目 `.cursor/mcp.json`、`.cursor/hooks.json`、Skill 和 always-on Rule。`sessionStart` 是 best-effort，不能替代 Rule；工具可用但启动块缺失时，Rule 要求首轮调用 `workspace_prepare`。
- **Devin Desktop**：全局配置默认 `%APPDATA%\Devin\mcp_config.json`；项目内安装 `.devin/skills`、`.devin/rules` 和安装清单。setup 只合并 `mcpServers.memweave` 并备份原文件，不安装 Hook。没有 Hook 意味着没有自动上下文注入或压缩快照，不等于核心能力降级；always-on Rule 仍是必需的启动通道。
- **generic**：只输出标准 `mcpServers.memweave` 配置，不自动安装 Skill/Rule。必须手动补齐启动通道。

遇到冲突、链接逃逸、环境不完整或网络错误时，按实际错误修复；不要删除已有环境、覆盖自定义配置或无限重试。

## 4. 通用 MCP 客户端

先安装 Python 包并初始化共享记忆库，再输出配置：

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m pip install .
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave init --home '<共享记忆库绝对路径>'
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave config --client generic --home '<共享记忆库绝对路径>'
```

把 `mcpServers.memweave` 合入客户端配置，并把 `src/memweave/skills/memweave-continuity/SKILL.md` 复制到客户端支持的技能目录。还必须安装一条 always-on Rule 或等价首轮宿主指令，要求首个项目动作调用 `workspace_prepare`。

## 5. 检查与交付

先核对每个安装命令的 JSON、退出码和冲突处理结果，不要只看“配置已写入”。

```powershell
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave check --client devin --root 'D:\my-project' --probe
& '<MemWeave 克隆>\.venv\Scripts\python.exe' -m memweave clients doctor --home '<共享记忆库绝对路径>'
```

`check --probe` 验证配置、SDK stdio、工具面和重启读取，不证明桌面客户端已经加载。`clients doctor` 默认只读，按“已发现项目根 × 选定客户端”报告完整覆盖；确认项目级缺口后才加 `--fix`，它会保留备份，且不会静默覆盖全局配置或人工修改的受管资源。

安装完成后：

1. 让用户重载客户端；Cursor/Devin 还需要确认 MCP 已启用。
2. 检查工具列表里出现 `memweave_*`；不要因为缺少裸名就判断 MCP 未加载。
3. 在真实项目请求中先读 `memweave_system_status`，再调用 `memweave_workspace_prepare(root=..., intent="inspect")`。
4. 确认版本、记忆库、项目身份和 22 工具。
5. 明确报告：已验证什么、未验证什么、哪些需要用户重载或手工确认。

部分宿主会给 MCP 工具名加服务前缀，Cursor 常见格式是 `project-0-<project>-memweave-memweave_*`；应按完整名称的后缀识别，调用时使用工具列表里的完整名称。

## 6. 更新

更新源码后重新运行 bootstrap 或执行 `pip install .`，再运行 `setup`。保留原记忆库和自定义内容；更新 Hook 定义后重新完成宿主信任。不要自动切换分支、丢弃本地修改或升级到用户未指定的版本。

详细逐项操作见本文前面的通用路径和 bootstrap 命令；产品介绍见 [README](README.md)，设计与运行说明见 [产品导览](docs/product-guide.html)。
