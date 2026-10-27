# 手动安装

当前源码入口为 https://github.com/haxsd/memweave ，私有访问使用已有 GitHub 登录。产品无需模型 API Key。以下命令在可信的源码克隆根目录执行，示例为 PowerShell。

## 1. 安装 Python 包

使用已核实的 Python 3.12+：

```powershell
& '<Python绝对路径>' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install .
```

已存在 `.venv` 时先检查并复用，不覆盖或删除。依赖安装需要可用网络。非 Windows 使用 `.venv/bin/python` 和相应的文件复制方式；本轮实际验证平台为 Windows/Python 3.13。

## 2. 选择记忆库并登记项目

```powershell
$memoryHome = Join-Path $env:USERPROFILE '.memweave'
$projectRoot = 'D:\my-project'
& '.\.venv\Scripts\python.exe' -m memweave init --home $memoryHome
$registration = @{root=$projectRoot; name='my-project'; operation_id='manual-register-my-project'} | ConvertTo-Json -Compress
$registration | & '.\.venv\Scripts\python.exe' -m memweave call project_register --home $memoryHome
```

两个客户端共享同一个 `$memoryHome`。登记只建立精确根目录，不创建任务或自动扫描。

## 3. 配置 MCP、Skill 与 Hooks

推荐手动运行接入命令完成安全合并：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave setup --client codex --root $projectRoot --home $memoryHome
```

Cursor 换为 `--client cursor`。命令保留其他 MCP、Hooks 和项目规则，备份变更前的内容；自定义受管内容有冲突时停止并提示合并。

如果希望逐项编辑，不使用 `setup`：

1. 运行 `config --client codex --home $memoryHome`，将输出的 `[mcp_servers.memweave]` 片段合并到项目 `.codex/config.toml`，保留其他配置，并在现有 `[features]` 中设置 `hooks = true`。不要重复 TOML 表。
2. 运行 `config --client codex --kind hooks --root $projectRoot --home $memoryHome`，将输出按事件合并到 `.codex/hooks.json`。保留已有 matcher groups，不重复追加 MemWeave 条目。
3. 将源码 `src/memweave/skills/memweave-continuity/` 复制到目标 `.agents/skills/memweave-continuity/`。先检查目标内容，自定义版本须自行合并。
4. 本机解释器和记忆库路径应保持本地。检查目标 Git 状态，把本次配置与备份放进本地忽略范围；不要将凭据或个人路径提交到业务仓库。`setup` 的 Codex 路径会安装局部 `.gitignore`。

Cursor 对应 `config --client cursor` 的 `mcpServers.memweave`，配置位置为 `.cursor/mcp.json`；Hooks 输出有 `version:1` 和六个事件，合并到 `.cursor/hooks.json`；Skill 位于 `.cursor/skills/memweave-continuity/`。安装 `setup` 还会生成 `.cursor/rules/memweave.mdc` 常驻入口；逐项配置时建立自己的规则，指向该 Skill 并要求实际项目工作先调用 `memweave_workspace_prepare`。

手工配置没有安装清单，Codex `check` 可按实际生成资源与显式 `--home` 检查；Cursor 受管 `check` 需要 `setup` 的清单。自定义资源可能使严格检查返回 needs_attention，应人工核对差异，不等于桌面一定加载失败。

## 4. 信任与验证

Codex 必须由宿主接受项目配置，再审查并信任 Hook 定义；支持此功能的 CLI 用 `/hooks`。不自动批准，不在普通配置中跳过信任。Cursor 在现有窗口中重载服务。

```powershell
& '.\.venv\Scripts\python.exe' -m memweave check --client codex --root $projectRoot --home $memoryHome --probe
```

协议自检通过后，在实际客户端读取状态和 `workspace_prepare(intent="inspect")`。确认版本、工具集、根目录与记忆库，再使用真实任务观察开始、执行检查点、完成提炼和后续复用。配置检查或 SDK 成功不能代替桌面自动触发验证。

## 5. 更新

更新源码至指定版本，在同一 `.venv` 重新 `pip install .`，再执行同样的 `setup` 与 `check --probe`。手工维护资源时重新生成并逐项合并。保留原记忆库；定义变更后重新审查 Codex Hooks。
