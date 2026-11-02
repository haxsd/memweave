import asyncio
import hashlib
import json
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from memweave import __version__
from memweave.diagnostics import DiagnosticError, Diagnostics
from memweave.integrations import (
    CURSOR_HOOK_EVENTS, CURSOR_LEGACY_HOOK_EVENTS, client_configuration, server_command,
)
from memweave.services import Service


UNIVERSAL_TOOLSET = "universal"
INSTALLED_TOOLSETS = (None, "core", "full", UNIVERSAL_TOOLSET)


def fingerprint(content):
    return hashlib.sha256(content).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class ProjectFiles:
    def __init__(self, root, directory=".cursor"):
        root = Path(root).expanduser()
        if not root.is_absolute() or not root.is_dir() or root.is_symlink() or root.is_junction():
            raise DiagnosticError("RECORD_INVALID", "Use an existing absolute project root without links.")
        self.root = root.resolve()
        self.cursor = self.root / directory

    def path(self, relative):
        path = self.cursor / relative
        if not path.resolve().is_relative_to(self.cursor.resolve()):
            raise DiagnosticError("SETUP_CONFLICT", "Project configuration path escapes .cursor.")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink() or parent.is_junction():
                raise DiagnosticError("SETUP_CONFLICT", "Linked configuration paths are not supported.")
        return path

    def read(self, relative):
        path = self.path(relative)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > 262144:
            raise DiagnosticError("CONFIG_INVALID", f"Invalid or oversized project file: {relative}.")
        return path.read_bytes()

    def load(self, relative):
        content = self.read(relative)
        if content is None:
            return {}
        try:
            value = json.loads(content.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError) as error:
            raise DiagnosticError("CONFIG_INVALID", f"Invalid UTF-8 JSON object: {relative}.") from error


def load_manifest(project):
    manifest = project.load("memweave.json")
    allowed_events = set(CURSOR_HOOK_EVENTS) | set(CURSOR_LEGACY_HOOK_EVENTS)
    if manifest and (
        manifest.get("format") != 1 or manifest.get("toolset") not in INSTALLED_TOOLSETS
        or manifest.get("root") != str(project.root)
        or not isinstance(manifest.get("home"), str)
        or not isinstance(manifest.get("resources"), dict)
        or not isinstance(manifest.get("hooks"), dict)
        or not isinstance(manifest.get("version"), str)
        or not isinstance(manifest.get("server"), dict)
    ):
        raise DiagnosticError("CONFIG_INVALID", "Invalid MemWeave installation manifest; preserve it before repairing.")
    if manifest and (
        not Path(manifest["home"]).is_absolute()
        or len(manifest["resources"]) != 2
        or "skills/memweave-continuity/SKILL.md" not in manifest["resources"]
        or not set(manifest["hooks"]).issubset(allowed_events)
        or any(not entries for entries in manifest["hooks"].values())
        or any(not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("command"), str) for entry in entries
        ) for entries in manifest["hooks"].values())
        or any(relative not in (
            "skills/memweave-continuity/SKILL.md", "rules/memweave.mdc", "rules/memweave-trial.mdc"
        ) or not isinstance(resource, dict) or type(resource.get("managed")) is not bool
            or not isinstance(resource.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", resource["sha256"])
            for relative, resource in manifest["resources"].items())
    ):
        raise DiagnosticError("CONFIG_INVALID", "Invalid nested installation metadata; preserve it before repairing.")
    return manifest


def selected_home(home, manifest):
    return Path(home or manifest.get("home") or Path.home() / ".memweave").expanduser().resolve()


def rule_text(root):
    return (
        "---\ndescription: MemWeave 项目记忆与接续。\nalwaysApply: true\n---\n\n"
        "# MemWeave\n\n"
        f"- 项目根目录为 `{root}`，遵守项目及子项目已有规则。\n"
        "- 每次新会话首次接续、创建任务或首次完成 MemWeave 工具调用后，在面向用户的回复开头"
        "显示一行 `MemWeave · 任务 <id> · <下一步>`；没有任务时显示 `MemWeave · 尚未建立任务`。"
        "不要每轮重复。\n"
        "- 任何状态、接续或“做到哪一步/下一步做什么”请求，必须先调用 "
        "memweave_workspace_prepare；这是只读请求，不得先读 "
        "Skill/PLAN/CURRENT_STATE/源码或运行全仓搜索，也不得用 CLI/Python 绕过 MCP 硬门，"
        "不得创建、编辑或删除文件。\n"
        "- 实施任务同样先 prepare；意图或任务归属有歧义时不按最近时间猜测，"
        "include_user 默认 false。\n"
        "- Hook 只负责会话开始、结束与中断清理；原话检索、任务绑定和检查点维护直接使用常驻 MCP，"
        "不要依赖逐次编辑或逐次工具调用的独立 Hook 进程。\n"
        "- 同一请求内的非破坏性步骤要连续完成，不因完成一个子步骤或保存检查点就交回控制权，"
        "也不要把中间汇报当成结束本轮的理由。只有请求完成、真实阻塞、必须决策或破坏性操作才返回；"
        "检查点和知识维护应静默执行，不向用户请求例行批准。\n"
        "- 按 .cursor/skills/memweave-continuity/SKILL.md 在重要发现、失败、阶段变化和验证后"
        "保存检查点，结束后核对验收并提炼，后续相关工作核对来源后复用。\n"
        "- 当前用户要求优先；不保存凭据或完整对话，不把未运行的验证标为通过。"
        "Hook 提示不能代替实际写入。日常维护尽量安静，不要求用户打开管理页。\n"
    ).encode("utf-8")


def owns_cursor_hook(entry):
    command = entry.get("command", "") if isinstance(entry, dict) else ""
    return isinstance(command, str) and bool(
        re.search(r"""['"]?memweave['"]?\s+['"]?hook\b""", command)
    )


def merged_ignore(project, relative, lines):
    content = project.read(relative)
    try:
        text = content.decode("utf-8-sig") if content is not None else ""
    except UnicodeError as error:
        raise DiagnosticError("CONFIG_INVALID", f"Cannot merge non-UTF-8 ignore file: {relative}.") from error
    additions = [line for line in lines if line not in text.splitlines()]
    if additions:
        text += ("" if not text or text.endswith("\n") else "\n") + "\n".join(additions) + "\n"
    return text.encode("utf-8")


def cursor_ignore_lines(manifest, rule_managed=None):
    lines = [
        "/.gitignore", "/mcp.json", "/hooks.json", "/memweave.json",
        "/memweave-backups/", "/skills/memweave-continuity/",
    ]
    if rule_managed is None:
        rule_managed = "rules/memweave.mdc" in manifest.get("resources", {})
    if rule_managed:
        lines.append("/rules/memweave.mdc")
    return lines


def installation_plan(project, home, toolset, manifest):
    mcp = project.load("mcp.json")
    servers = mcp.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise DiagnosticError("CONFIG_INVALID", "mcpServers must be an object.")
    server = server_command(home)
    existing = servers.get("memweave")
    if existing is not None and existing not in (server, manifest.get("server")):
        raise DiagnosticError("SETUP_CONFLICT", "MemWeave MCP entry differs; preserve or merge the existing entry.")
    servers["memweave"] = server
    configuration = project.load("hooks.json")
    if configuration.get("version", 1) != 1:
        raise DiagnosticError("CONFIG_INVALID", "Unsupported Cursor Hooks version.")
    configuration["version"] = 1
    hooks = configuration.setdefault("hooks", {})
    if not isinstance(hooks, dict) or any(
        not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries)
        for entries in hooks.values()
    ):
        raise DiagnosticError("CONFIG_INVALID", "Hook events must contain arrays of objects.")
    generated = json.loads(client_configuration("cursor", "hooks", home))["hooks"]
    previous = manifest.get("hooks", {})
    for event in set(previous) - set(generated):
        retained = []
        for entry in hooks.get(event, []):
            if entry in previous[event]:
                continue
            if owns_cursor_hook(entry):
                raise DiagnosticError("SETUP_CONFLICT", f"Modified or unknown MemWeave Hook: {event}.")
            retained.append(entry)
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    for event, entries in generated.items():
        known = previous.get(event, []) + entries
        retained = []
        for entry in hooks.get(event, []):
            if entry in known:
                continue
            if owns_cursor_hook(entry):
                raise DiagnosticError("SETUP_CONFLICT", f"Modified or unknown MemWeave Hook: {event}.")
            retained.append(entry)
        hooks[event] = retained + entries
    skill_path = "skills/memweave-continuity/SKILL.md"
    rule_path = "rules/memweave.mdc"
    external_rule = (
        rule_path not in manifest.get("resources", {}) and project.read(rule_path) is None
        and project.read("rules/memweave-trial.mdc") is not None
    )
    if external_rule:
        rule_path = "rules/memweave-trial.mdc"
    resources = {
        skill_path: files("memweave").joinpath("skills", "memweave-continuity", "SKILL.md").read_bytes(),
        rule_path: project.read(rule_path) if external_rule else rule_text(project.root),
    }
    ownership = {}
    ignored = cursor_ignore_lines(manifest, rule_managed=not external_rule)
    desired = {
        ".gitignore": merged_ignore(project, ".gitignore", ignored),
        "mcp.json": json_bytes(mcp),
        "hooks.json": json_bytes(configuration),
    }
    for relative, content in resources.items():
        current = project.read(relative)
        old = manifest.get("resources", {}).get(relative, {})
        managed = not (relative == rule_path and external_rule)
        if managed and current is not None and current != content and fingerprint(current) != old.get("sha256"):
            raise DiagnosticError("SETUP_CONFLICT", f"Resource was modified; merge it before setup: {relative}.")
        desired[relative] = content
        ownership[relative] = {"sha256": fingerprint(content), "managed": managed}
    updated = dict(
        format=1, version=__version__, root=str(project.root), home=str(home),
        toolset=UNIVERSAL_TOOLSET,
        server=server, hooks=generated, resources=ownership,
    )
    desired["memweave.json"] = json_bytes(updated)
    return {relative: (project.read(relative), content) for relative, content in desired.items()
            if project.read(relative) != content}


def replace_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".memweave-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(project, plan, backup_prefix="memweave-backups"):
    backup = backup_prefix + "/" + uuid4().hex if any(old is not None for old, _ in plan.values()) else None
    for relative, (old, content) in plan.items():
        if project.read(relative) != old:
            raise DiagnosticError("SETUP_CONFLICT", "Project configuration changed during setup; retry.")
        if backup and old is not None:
            replace_file(project.path(backup + "/" + relative), old)
    applied = []
    try:
        for relative, (old, content) in plan.items():
            if project.read(relative) != old:
                raise DiagnosticError("SETUP_CONFLICT", "Project configuration changed during setup; retry.")
            replace_file(project.path(relative), content)
            applied.append(relative)
    except (OSError, DiagnosticError):
        for relative in reversed(applied):
            old, content = plan[relative]
            if project.read(relative) == content:
                if old is None:
                    project.path(relative).unlink()
                else:
                    replace_file(project.path(relative), old)
        raise
    return str(project.path(backup)) if backup else None


def setup_cursor(root, home=None, toolset=None):
    project = ProjectFiles(root)
    manifest = load_manifest(project)
    home = selected_home(home, manifest)
    installation_plan(project, home, toolset, manifest)
    project.cursor.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(project.path(".memweave-setup.lock"), timeout=3):
            manifest = load_manifest(project)
            plan = installation_plan(project, home, toolset, manifest)
            service = Service(home)
            service.initialize()
            service.project_register(
                str(project.root), project.root.name,
                "setup-" + fingerprint(str(project.root).encode("utf-8")),
            )
            backup = apply_plan(project, plan)
    except Timeout as error:
        raise DiagnosticError("LOCK_BUSY", "Another setup is running; retry after it finishes.") from error
    return dict(state="installed" if plan else "up_to_date", version=__version__, root=str(project.root),
                home=str(home), toolset=UNIVERSAL_TOOLSET, tool_count=22,
                changed_files=list(plan), backup_directory=backup, desktop_verification="unverified")


async def probe_installation(server, root):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    async with asyncio.timeout(25):
        async with Client(StdioServerParameters(**server, cwd=str(root)), read_timeout_seconds=10) as client:
            tools = (await client.list_tools()).tools
            status = await client.call_tool("memweave_system_status")
            prepared = await client.call_tool("memweave_workspace_prepare", {"root": str(root), "intent": "inspect"})
            if status.is_error or prepared.is_error:
                raise DiagnosticError("PROBE_FAILED", "Configured service rejected the read-only self-check.")
            data = status.structured_content["data"]
            workspace = prepared.structured_content["data"]
            if workspace["state"] != "ready":
                raise DiagnosticError("PROBE_FAILED", "Configured service cannot prepare the registered workspace.")
            return dict(version=data["version"], toolset=data["toolset"], tool_count=len(tools),
                        home=data["home"], active_task_count=len(workspace["tasks"]))


def check_installation(root, home=None, probe=False):
    project = ProjectFiles(root)
    checks = []

    def check(name, passed, message, fix=None):
        checks.append(dict(name=name, state="ok" if passed else "error", message=message,
                           fix="" if passed else fix or "使用同一记忆库重新运行 memweave setup --root <项目目录>。"))

    try:
        manifest = load_manifest(project)
        home = selected_home(home, manifest)
        check("installation", bool(manifest), "项目安装清单存在。")
        if not manifest:
            return dict(state="needs_attention", version=__version__, root=str(project.root), home=str(home),
                        checks=checks, protocol_verification="not_run", desktop_verification="unverified")
        check("version", manifest["version"] == __version__, "接入资源版本与当前安装包一致。")
        server = manifest.get("server", {})
        check("server_command", server == server_command(home),
              "服务命令、参数和环境必须匹配当前 MemWeave 安装环境。")
        check("interpreter", isinstance(server.get("command"), str) and Path(server["command"]).is_file(),
              "配置中的 Python 解释器路径存在。")
        check("home", str(home) == manifest["home"], "当前记忆库与安装清单一致。")
        mcp = project.load("mcp.json")
        check("mcp", mcp.get("mcpServers", {}).get("memweave") == server, "项目 MCP 配置与安装清单一致。")
        hooks = project.load("hooks.json")
        expected_hooks = json.loads(client_configuration("cursor", "hooks", home))["hooks"]
        check(
            "hooks",
            manifest["hooks"] == expected_hooks
            and hooks.get("version") == 1
            and isinstance(hooks.get("hooks"), dict)
            and all(
                all(entry in hooks["hooks"].get(event, []) for entry in entries)
                for event, entries in expected_hooks.items()
            )
            and all(
                not owns_cursor_hook(entry) or entry in expected_hooks.get(event, [])
                for event, entries in hooks.get("hooks", {}).items() for entry in entries
            ),
            "受管 Hook 定义完整、与安装清单一致且没有旧的高频事件。",
        )
        ignore_content = project.read(".gitignore")
        try:
            ignore_text = ignore_content.decode("utf-8-sig") if ignore_content is not None else ""
        except UnicodeError:
            ignore_text = ""
        ignore_lines = set(ignore_text.splitlines())
        check(
            "gitignore",
            all(line in ignore_lines for line in cursor_ignore_lines(manifest)),
            "Cursor 本机受管文件与备份目录已加入 .cursor/.gitignore。",
        )
        expected_skill = files("memweave").joinpath(
            "skills", "memweave-continuity", "SKILL.md"
        ).read_bytes()
        expected_rule = rule_text(project.root)
        expected_hashes = {
            "skills/memweave-continuity/SKILL.md": fingerprint(expected_skill),
            "rules/memweave.mdc": fingerprint(expected_rule),
        }
        manifest_resources_match = all(
            resource["sha256"]
            == expected_hashes.get(relative, fingerprint(project.read(relative) or b""))
            for relative, resource in manifest["resources"].items()
        )
        check(
            "manifest",
            manifest["version"] == __version__
            and manifest["server"] == server
            and manifest["hooks"] == expected_hooks
            and manifest["home"] == str(home)
            and manifest.get("toolset") == UNIVERSAL_TOOLSET
            and manifest_resources_match,
            "安装清单与当前服务、Hook、通用工具面和包内资源一致。",
        )
        for relative, resource in manifest["resources"].items():
            content = project.read(relative)
            name = "skill" if relative.endswith("SKILL.md") else "rule"
            expected = expected_hashes.get(relative)
            passed = content is not None and (
                not resource["managed"]
                or (fingerprint(content) == expected if expected else
                    fingerprint(content) == resource["sha256"])
            )
            check(name, passed, "资源存在且受管内容与当前包一致。",
                  "资源缺失时运行 setup；如有自定义修改，请先备份合并，再运行 setup。")
        status = Diagnostics(home).status()["data"]
        check("memory_home", status["initialized"] and status["task_memory"] == "available",
              "记忆库已经初始化且业务存储可用。")
        if status["initialized"] and status["task_memory"] == "available":
            prepared = Service(home).workspace_prepare(str(project.root), intent="inspect")
            check("project", prepared["state"] == "ready", "项目根目录或同仓库 worktree 已登记。")
    except (DiagnosticError, KeyError, TypeError, AttributeError) as error:
        check("configuration", False, error.code if isinstance(error, DiagnosticError) else "Invalid installation data.")
    output = dict(state="ready" if all(item["state"] == "ok" for item in checks) else "needs_attention",
                  version=__version__, root=str(project.root), home=str(home) if home else None,
                  toolset=UNIVERSAL_TOOLSET if "manifest" in locals() and manifest else None,
                  tool_count=22,
                  checks=checks, protocol_verification="not_run", desktop_verification="unverified",
                  required_actions=[
                      "在 Cursor 当前项目首次启用一次 MemWeave MCP；同一项目后续会话应复用该开关。"
                      "若 memweave_* 工具缺失，先检查项目级 MCP 开关；"
                      "启动 Hook 的会话状态只表示上下文已由 Hook 输出，不代表模型已收到；"
                      "工具可用时首轮调用 workspace_prepare。"
                  ])
    if probe and output["state"] == "ready":
        try:
            verified = asyncio.run(probe_installation(server, project.root))
            valid = (verified["version"] == __version__ and verified["toolset"] == UNIVERSAL_TOOLSET
                     and verified["tool_count"] == output["tool_count"] and Path(verified["home"]) == home)
            check("protocol", valid, "Configured service version, universal tool surface, root and memory home verified.")
            if valid:
                output.update(protocol_verification="sdk_stdio", active_task_count=verified["active_task_count"])
        except Exception:
            check("protocol", False, "Configured stdio service could not complete the self-check.")
        output["state"] = "ready" if all(item["state"] == "ok" for item in checks) else "needs_attention"
    return output
