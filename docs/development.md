# 本地开发与验证

> 本文描述公开仓库的开发流程。内部阶段记录、真实本机日志和未公开证据不在这里维护。

## 环境

需要 Python 3.12+。推荐在仓库根目录使用独立虚拟环境：

```powershell
& '<Python 3.12+ 绝对路径>' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.windows-py313.txt
& '.\.venv\Scripts\python.exe' -m pip install --no-deps -e .
```

当前锁文件对应 Windows / Python 3.13；其他平台或版本需要重新解析依赖并验证。不要把 `.venv/`、客户端配置、记忆库或证据日志提交进仓库。

## 测试

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
& '.\.venv\Scripts\python.exe' -m compileall -q src tests scripts
git diff --check
```

定向修改先运行相关测试；影响安装、客户端配置、存储、MCP 工具面或 Hook 时，再运行全量回归。CI 使用 Python 3.13 / Ubuntu 运行同一套 unittest。

## 接入与启动

开发时可以直接用源码启动：

```powershell
& '.\.venv\Scripts\python.exe' -m memweave serve --home '<临时记忆库绝对路径>'
```

安装器与客户端配置见 [客户端接入](integrations.md)。`scripts/bootstrap.py` 复用或创建仓库内 `.venv`，安装当前源码，执行 `setup` 和 `check --probe`；先加 `--dry-run --json` 可以只看计划。

协议进程通过、配置写入、桌面加载和模型遵从是不同证据。不要把 `check --probe` 写成桌面已验收。

## 本地管理页

```powershell
& '.\.venv\Scripts\python.exe' -m memweave ui --home '<记忆库绝对路径>' --port 8765 --root '<项目绝对路径>'
```

管理页是本机单用户辅助入口，不是公网服务，也不代替 Agent 的日常记忆维护。

## 构建

```powershell
& '.\.venv\Scripts\python.exe' -m pip wheel . --no-deps -w dist
```

构建前确认没有把 `.venv`、客户端配置、记忆库、证据或生成文件带入 wheel。第三方来源和许可证义务见 [实现参考](design-references.md) 与 [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)。

## 代码边界

- 保持一个 MCP、一个 Skill 和一个启动通道，不新增第二套运行时。
- 不把逐轮原话、完整对话、命令输出或凭据写入任务/知识。
- Markdown 是权威记录，SQLite 是派生索引。
- 客户端是适配器，不是能力等级；不要为了某个客户端改变 22 工具核心。
- 受管安装修改客户端配置时必须保留已有内容、备份并报告冲突。
- 未运行的测试和未观察的客户端行为保持未验证。
