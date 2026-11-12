# MemWeave × DeepSeek Harness（实验性适配）

> 状态：实验性、本机验证过。2026-11-08 在 DeepSeek Harness Desktop `0.2.2-rc.1` 上完成真实接入：`memweave_*` 22 个工具可用，`memweave_system_status` 与 `workspace_prepare(resume)` 成功，使用共享 vault `%USERPROFILE%\.memweave`。
>
> 这是宿主适配层，不改变 MemWeave 核心：仍是同一个 22 工具 MCP、同一任务/知识模型、同一项目身份和 Markdown 权威。DSH 没有 Codex 式的生命周期 Hook，启动通道由 always-on Skill/宿主指令保证；压缩恢复依赖重新 `workspace_prepare`。

## 为什么需要额外步骤

DSH 通过 Cordis 插件树挂载 MCP：MCP server 由 `@deepseek-ai/dsh-mcp-client` 以 stdio 子进程启动。DSH 的 MCP 子进程运行在 **Low integrity（低完整性）沙箱**中，而默认的 `%USERPROFILE%\.memweave` 是 Medium integrity。结果是 server 无法创建 `runtime/write.lock`，所有工具返回 `LOCK_BUSY` —— 这不是锁竞争，而是 `filelock` 把 Windows `EACCES` 误报成超时。

解决办法是给 vault 设置 **Low mandatory label**，让 DSH 的低完整性 MCP 子进程可以写入。这是本机单用户场景下的安全权衡；如果你不希望放宽 vault 完整性，只能改用 DSH 本地 vault（会失去与 Codex/Cursor/Devin 的共享连续性），或等待 DSH 提供 MCP 进程提权配置。

```powershell
# 让 DSH 的 Low integrity MCP 子进程可写共享 vault
icacls "$env:USERPROFILE\.memweave" /setintegritylevel (OI)(CI)Low /T

# 可选回退：改回 Medium（默认文件没有显式标签；Medium 也允许 Medium 进程写）
icacls "$env:USERPROFILE\.memweave" /setintegritylevel (OI)(CI)Medium /T
```

## 安装

前置：仓库 `.venv` 已安装当前源码，vault 已初始化（`python -m memweave init --home ...`），并已有一个共享 vault。

```powershell
# 1. 生成 DSH 本地插件包（默认安装到 %LOCALAPPDATA%\MemWeave\dsh-plugin）
#    默认 PowerShell 执行策略可能禁止直接运行 .ps1，故用 Bypass 方式调用。
powershell -NoProfile -ExecutionPolicy Bypass -File .\integrations\dsh\install.ps1 `
  -Python "$PWD\.venv\Scripts\python.exe" `
  -HomeDir "$env:USERPROFILE\.memweave" `
  -SetVaultIntegrity

# 2. 安装 always-on Skill（DSH 会扫描用户级技能目录）
New-Item -ItemType Directory -Force "$env:USERPROFILE\.agents\skills\memweave-continuity" | Out-Null
Copy-Item -Recurse -Force '.\src\memweave\skills\memweave-continuity\*' "$env:USERPROFILE\.agents\skills\memweave-continuity\"

# 3. 在 DSH 会话中注册 bundle（由 Agent 调用 DSH 的 plugin_manager）
#    target = link:<install.ps1 输出的 InstallDir>
#    action = install_bundle
```

如果没有 Agent 可调用 `plugin_manager`，可在 DSH 的插件管理界面添加本地 link；DSH 升级后界面可能变化。

注册成功后，DSH 会在当前会话热挂载 MCP（本机验证无需重启 GUI），模型工具列表出现：

```text
mcp__memweave__memweave_system_status
mcp__memweave__memweave_workspace_prepare
... 共 22 个
```

首次项目动作仍应遵循 MemWeave 启动契约：`system_status` → `workspace_prepare(root, intent)`；不要因为工具出现就跳过恢复。

## 验证

在 DSH 会话内：

1. 工具列表中出现 22 个 `mcp__memweave__*` 工具。
2. `mcp__memweave__memweave_system_status` 返回 `status=ok`、`home=%USERPROFILE%\.memweave`、`tool_count=22`。
3. `mcp__memweave__memweave_workspace_prepare(root=<项目绝对路径>, intent="resume")` 返回 `ready` 或 `no_active_task`；`resume` 成功时返回 `execution.claim_id`。
4. `list_mcp_resources` 的服务器列表包含 `memweave`（DSH 资源读取走同一插件配置）。
5. 创建/恢复任务后写一次检查点，确认 Markdown 出现在 `%USERPROFILE%\.memweave\checkpoints\`。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有工具 `LOCK_BUSY`，CLI 也失败 | vault 是 Medium integrity，DSH 子进程是 Low | 执行上文 `icacls ... Low` 并`icacls` 验证 `Mandatory Label\Low`；重新调用工具 |
| 插件激活报 `TypeError: Invalid effect` | DSH 插件 `apply` 返回了 Cordis fiber/effect | `apply` 只 `await connection`，不要 `return connection`（模板已修正） |
| 工具列表没有 memweave | bundle 未安装/未应用，或 MCP worker 未重载 | 用 `plugin_manager list_plugins` 查 `mcp-memweave` 是否存在；必要时移除后重装，并重载 DSH 宿主 |
| 同一 server 进程越积越多 | 失败重试期间 DSH 反复拉起 | 先 `remove_bundle` 停止重拉，杀掉残留 `python -m memweave serve`，再装一次 |
| pnpm 提示 peer dependency 未解析 | `@deepseek-ai/dsh-mcp-client` 是 DSH 内置桥，不在 profile node_modules | 属预期提示；DSH 宿主仍能解析该 peer |

## 边界

- DSH 当前的 MCP server 是 Low integrity；这与 Codex/Cursor 由各自宿主启动 MCP 的方式不同。vault 的 Low label 会影响所有低完整性进程，不只是 DSH。
- 适配依赖 DSH 的 `@deepseek-ai/dsh-mcp-client` 接口和 Cordis patch 格式；DSH 升级后需重新验证。
- DSH 没有 MemWeave 生命周期 Hook：不会自动注入启动上下文，也不保存压缩前快照；压缩/重启后必须重新 `workspace_prepare`。
- 本适配已在本机验证工具与协议；Skill 的模型遵从与统计收益仍按 MemWeave 的常规边界记录，不因安装成功而宣称。
