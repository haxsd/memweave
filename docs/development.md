# 本地开发与验证

## 当前工程范围

本轮源码修订为 0.2.4，支持 Codex / Cursor 项目安装、更新和检查，以及 Agent 引导与手动安装。沿用 Python、MCP + Skills + Hooks、Markdown + SQLite、宿主负责理解提炼的选择；新增 tomlkit 0.13.3 用于保留 Codex TOML 注释和其他配置。

已有工程能力包括任务与检查点、按范围保存的知识、候选与显式启用、词法检索、纠正与遗忘、文件结构和有来源的架构图谱，以及版本、内容指纹、跨进程锁与中断恢复。浏览器管理页保留为可选辅助；探针和 Hook 收据仅作诊断，Hook 返回的上下文与跟进仍须宿主实际使用。

运行报告保存在本机开发区，不随源码分发。Codex 与 Cursor 安装入口以当前源码和实际测试为准；真实桌面持续体验与模型收益仍待验收，参见 [产品规格](specs/2026-10-27-product-realignment.md)。

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
& '.\.venv\Scripts\python.exe' -m memweave init --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave config --client cursor --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave config --client codex --home '.\work\runtime\memory home'
```

配置输出与包内原创 Skill 的手动复制方式见 [接入说明](integrations.md)，宿主内四时点调用顺序见 [Agent 指引](agent-guide.md)。没有经过真实事件验证的自动化时，主动使用 MCP/Skill 流程。

只读 `workspace_prepare(root, intent="inspect", task_id=None, query="", include_user=False, budget=12000)` 供 MCP 与 CLI 共用。根目录必须存在、为绝对路径且精确匹配注册记录；不隐式注册、创建任务或扫描。`inspect/new` 不选任务，`resume` 只恢复显式任务或唯一活跃任务；多个活跃任务返回 `task_selection_required`，未注册返回 `registration_required`。

CLI 使用 `call` 的 `workspace_prepare` action，参数为同一 JSON 对象。Windows 路径与 JSON 输入方式见 [API 说明](api.md)，不要依赖客户端的启动工作目录来推断目标根目录。

返回 `state`、`project`、`tasks`、`pending_consolidation` 与 `context`；任务简表有上限及截断提示。上下文组合受总字符预算约束，纳入选定任务摘要、已启用知识和有效局部图谱，给出 `included_sources`、截断与图谱覆盖/新鲜度。用户范围知识必须通过 `include_user` 显式选择，候选与来源漂移架构不会作为有效内容纳入。

## 诊断、演示与可选页面

按需执行：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave doctor --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave probe --home '.\work\runtime\memory home'
& '.\.venv\Scripts\python.exe' -m memweave demo --home '.\work\runtime\memory home' --root .
& '.\.venv\Scripts\python.exe' -m memweave ui --home '.\work\runtime\memory home'
```

`init` 创建配置、索引、操作日志和墓碑表，现有阶段 0 数据库自动升级到业务 schema 2。再次调用保留现有记录；遇到未知配置或数据库版本时报错。`doctor` 不初始化缺失的目录。配置 schema 仍为 1。

`probe` 启动第一个真实 stdio 子进程，完成握手、列工具和标记写入，随后关闭它；启动第二个进程读取同一个标记。每次生成唯一 `operation_id`，保留该条诊断记录。成功结果的 `verification_source` 为 `sdk_stdio`，不表示任何桌面客户端已经加载。

`serve` 需要已初始化的库，启动时迁移并恢复中断操作；通常由客户端按生成的配置启动，标准输出只用于 MCP 协议。`call` 要求先初始化，参数与冲突恢复见 [API 说明](api.md)。

`demo` 是两个独立 MCP 进程的工程演示，会留下带演示标记的数据；不代表真实桌面 Agent 已适时记录或主动使用知识。`ui` 可以初始化空库，但页面不是日常工作或接入 MCP 的前置条件。

## 测试与构建

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.test_services tests.test_context -v
& '.\.venv\Scripts\python.exe' -m unittest tests.test_mcp -v
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv\Scripts\python.exe' -m pip wheel --no-deps --wheel-dir dist .
```

测试使用临时目录与真实 SQLite、Python 子进程、HTTP 与 MCP SDK；覆盖中文、空格、单引号路径、幂等、并发写入、真实进程突然退出后的恢复、外部编辑冲突、遗忘防复活、范围隔离、中文检索、来源失效、Windows junction、HTTP 本机边界、stdio 重启和 legacy 协议。符号链接测试可能因 Windows 权限跳过，junction 有独立真实测试。

测试中的故意冲突、找不到探针和参数越界会由 SDK 在 stderr 输出预期错误；以 unittest 结果与进程退出码判断通过。Windows 测试还实际执行生成的 PowerShell Hook 命令。

本轮需验收精确根目录、任务歧义隔离、`new` 不恢复旧任务、准备入口无隐式写入、用户范围显式选择、长历史下知识与结构的预算分配、漂移架构排除、实际纳入的来源标识，以及真实 MCP 服务重启恢复。命令清单表示验证方法，是否通过以本轮执行记录为准。

## 代码边界

| 文件 | 职责 |
|---|---|
| `src/memweave/diagnostics.py` | 初始化、状态、SQLite 探针与事件收据 |
| `src/memweave/integrations.py` | 生成客户端配置与正确引用的启动参数 |
| `src/memweave/installation.py` | Cursor 项目安装更新、清单、备份与配置/协议检查 |
| `src/memweave/codex_installation.py` | Codex TOML 与 Hook 合并、Skill、安装清单和检查 |
| `src/memweave/server.py` | MCP 工具声明、参数边界与业务错误转换 |
| `src/memweave/probe.py` | 有界 stdio 握手和重启读取 |
| `src/memweave/cli.py` | argparse 入口、JSON 输出与 Hook 输入 |
| `src/memweave/hooks.py` | Cursor 上下文注入、会话任务关联、编辑标记与有界跟进 |
| `src/memweave/codex_hooks.py` | Codex 原生事件、压缩恢复、工作观察与有界续轮 |
| `scripts/bootstrap.py` | 克隆内准备环境、安装包、setup 与协议自检 |
| `src/memweave/storage.py` | Markdown 权威记录、锁、操作日志、索引与恢复 |
| `src/memweave/services.py` | 项目、任务、知识与上下文应用服务 |
| `src/memweave/context.py` | 本轮上下文组合器：总字符预算、知识与局部图谱、实际来源 |
| `src/memweave/project_map.py` | 有界扫描、文件证据、架构关系与失效 |
| `src/memweave/web.py` | 本机 HTTP 与随机令牌访问 |
| `src/memweave/static/index.html` | 可选中文管理页 |
| `src/memweave/demo.py` | 两个独立 MCP 服务进程的完整演示 |

构建本轮 wheel 后，应在独立环境安装锁文件与实际构建的 wheel，离开源码目录核对 `--version`、新入口、上下文与真实 MCP 重启，再执行 `demo`、`probe` 和包资源检查。安装包应包含原创 Skill；资源存在不能证明目标客户端已安装或触发它。可选页面没有 npm 构建或前端运行依赖。

不把 `clients.*.desktop_verification` 手动改为“已通过”来代替桌面验收。工程测试通过后仍需真实宿主的四时点流程观察，再以无记忆、仅任务记忆、完整记忆与图谱进行公平对照；不能用协议或页面结果代替核心收益证据。
