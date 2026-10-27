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
from memweave.integrations import client_configuration, server_command
from memweave.services import Service


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
    if manifest and (
        manifest.get("format") != 1 or manifest.get("toolset") not in ("core", "full")
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
        or set(manifest["hooks"]) != {
            "sessionStart", "sessionEnd", "preCompact", "afterMCPExecution", "afterFileEdit", "stop",
        }
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
        "- 开始实际开发任务时，调用 memweave_workspace_prepare，明确 new/resume/inspect；"
        "意图或任务归属有歧义时不按最近时间猜测，include_user 默认 false。\n"
        "- 按 .cursor/skills/memweave-continuity/SKILL.md 在重要发现、失败、阶段变化和验证后"
        "保存检查点，结束后核对验收并提炼，后续相关工作核对来源后复用。\n"
        "- 当前用户要求优先；不保存凭据或完整对话，不把未运行的验证标为通过。"
        "Hook 提示不能代替实际写入。日常维护尽量安静，不要求用户打开管理页。\n"
    ).encode("utf-8")


def installation_plan(project, home, toolset, manifest):
    mcp = project.load("mcp.json")
    servers = mcp.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise DiagnosticError("CONFIG_INVALID", "mcpServers must be an object.")
    server = server_command(home, toolset)
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
    for event, entries in generated.items():
        known = previous.get(event, []) + entries
        retained = []
        for entry in hooks.get(event, []):
            if entry in known:
                continue
            command = entry.get("command", "")
            if isinstance(command, str) and re.search(r"""['"]?memweave['"]?\s+['"]?hook\b""", command):
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
    desired = {"mcp.json": json_bytes(mcp), "hooks.json": json_bytes(configuration)}
    for relative, content in resources.items():
        current = project.read(relative)
        old = manifest.get("resources", {}).get(relative, {})
        managed = not (relative == rule_path and external_rule)
        if managed and current is not None and current != content and fingerprint(current) != old.get("sha256"):
            raise DiagnosticError("SETUP_CONFLICT", f"Resource was modified; merge it before setup: {relative}.")
        desired[relative] = content
        ownership[relative] = {"sha256": fingerprint(content), "managed": managed}
    updated = dict(
        format=1, version=__version__, root=str(project.root), home=str(home), toolset=toolset,
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


def setup_cursor(root, home=None, toolset="core"):
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
                home=str(home), toolset=toolset, tool_count=20 if toolset == "core" else 27,
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
        check("server_command", server == server_command(home, manifest["toolset"]),
              "服务命令、参数和环境必须匹配当前 MemWeave 安装环境。")
        check("interpreter", isinstance(server.get("command"), str) and Path(server["command"]).is_file(),
              "配置中的 Python 解释器路径存在。")
        check("home", str(home) == manifest["home"], "当前记忆库与安装清单一致。")
        mcp = project.load("mcp.json")
        check("mcp", mcp.get("mcpServers", {}).get("memweave") == server, "项目 MCP 配置与安装清单一致。")
        hooks = project.load("hooks.json")
        expected_hooks = json.loads(client_configuration("cursor", "hooks", home))["hooks"]
        check("hooks", manifest["hooks"] == expected_hooks and hooks.get("version") == 1
              and isinstance(hooks.get("hooks"), dict) and all(
            all(entry in hooks["hooks"].get(event, []) for entry in entries)
            for event, entries in expected_hooks.items()
        ), "六类受管 Hooks 配置完整。")
        for relative, resource in manifest["resources"].items():
            content = project.read(relative)
            name = "skill" if relative.endswith("SKILL.md") else "rule"
            passed = content is not None and (
                not resource["managed"] or fingerprint(content) == resource["sha256"]
            )
            check(name, passed, "资源存在且受管内容未被修改。",
                  "资源缺失时运行 setup；如有自定义修改，请先备份合并，再运行 setup。")
        status = Diagnostics(home).status()["data"]
        check("memory_home", status["initialized"] and status["task_memory"] == "available",
              "记忆库已经初始化且业务存储可用。")
        if status["initialized"] and status["task_memory"] == "available":
            prepared = Service(home).workspace_prepare(str(project.root), intent="inspect")
            check("project", prepared["state"] == "ready", "精确项目根目录已登记。")
    except (DiagnosticError, KeyError, TypeError, AttributeError) as error:
        check("configuration", False, error.code if isinstance(error, DiagnosticError) else "Invalid installation data.")
    output = dict(state="ready" if all(item["state"] == "ok" for item in checks) else "needs_attention",
                  version=__version__, root=str(project.root), home=str(home) if home else None,
                  toolset=manifest.get("toolset") if "manifest" in locals() else None,
                  tool_count=20 if "manifest" in locals() and manifest.get("toolset") == "core" else 27,
                  checks=checks, protocol_verification="not_run", desktop_verification="unverified")
    if probe and output["state"] == "ready":
        try:
            verified = asyncio.run(probe_installation(server, project.root))
            valid = (verified["version"] == __version__ and verified["toolset"] == output["toolset"]
                     and verified["tool_count"] == output["tool_count"] and Path(verified["home"]) == home)
            check("protocol", valid, "Configured service version, toolset, root and memory home verified.")
            if valid:
                output.update(protocol_verification="sdk_stdio", active_task_count=verified["active_task_count"])
        except Exception:
            check("protocol", False, "Configured stdio service could not complete the self-check.")
        output["state"] = "ready" if all(item["state"] == "ok" for item in checks) else "needs_attention"
    return output
