import asyncio
import json
import os
import re
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from memweave import __version__
from memweave.diagnostics import CLIENT_BOOTSTRAP_CHANNELS, DiagnosticError, Diagnostics
from memweave.installation import (
    ProjectFiles, CORRECTION_CHECKPOINT, STARTUP_FALLBACK, apply_plan, fingerprint,
    json_bytes, merged_ignore, probe_installation, replace_file, selected_home,
)
from memweave.integrations import server_command
from memweave.services import Service


MANIFEST = "memweave.json"
SKILL = "skills/memweave-continuity/SKILL.md"
RULE = "rules/memweave.md"
REQUIRED_ACTIONS = [
    "重启 Devin Desktop，使全局 mcp_config.json 中的 MemWeave 服务重新加载。",
    "在 Devin 中打开目标项目，确认 memweave_* 工具出现。",
    "首个状态或实施请求先调用 workspace_prepare；配置检查不等于 Devin 桌面已加载。",
]


def devin_config_path(global_base=None):
    base = Path(global_base or Path.home()).expanduser().resolve()
    directory = (
        base / "AppData" / "Roaming" / "devin"
        if os.name == "nt"
        else base / ".config" / "devin"
    )
    return directory / "mcp_config.json"


def report(project, home, **fields):
    return dict(
        version=__version__, client="devin", root=str(project.root), home=str(home),
        toolset="universal", tool_count=22, mcp_scope="global",
        bootstrap_channel=CLIENT_BOOTSTRAP_CHANNELS["devin"],
        protocol_verification="not_run", desktop_verification="unverified",
        hooks_installed=False, required_actions=list(REQUIRED_ACTIONS), **fields,
    )


def rule_text(root):
    return (
        "---\ndescription: MemWeave 项目记忆与接续。\ntrigger: always_on\n---\n\n"
        "# MemWeave\n\n"
        f"- 项目根目录为 `{root}`，遵守项目及子项目已有规则。\n"
        "- MemWeave 的 22 工具核心、任务与知识模型、项目身份与其他 MCP 客户端相同；"
        "本 always-on Rule 是 Devin 的启动通道，负责要求首轮调用 workspace_prepare。"
        "Devin 没有生命周期 Hook，因此没有自动上下文注入或压缩快照；核心相同，"
        "但自动恢复能力弱于有 Hook 的宿主，必须靠 always-on Rule 首轮回退。\n"
        "- 每次新会话首次接续、创建任务或首次完成 MemWeave 工具调用后，在面向用户的回复开头"
        "显示一行 `MemWeave · 任务 <id> · <下一步>`；没有任务时显示 `MemWeave · 尚未建立任务`。"
        "不要每轮重复。\n"
        "- 宿主可能给工具名加前缀（Cursor 常见 "
        "`project-0-<project>-memweave-memweave_*`）；判断可用性时看完整名称是否以 "
        "memweave_system_status / memweave_workspace_prepare 结尾，调用时使用列表中的完整名称。"
        "不要把“裸名未出现”误判为 MCP 未加载。\n"
        "- 任何状态、接续或“做到哪一步/下一步做什么”请求，以及首轮/恢复路径，必须先确认当前工具列表已出现 "
        "memweave_system_status → memweave_workspace_prepare（必要时再 memweave_task_read 当前任务）再调用；"
        "若当前工具列表尚未出现 memweave_workspace_prepare，不得凭本规则凭空发起工具调用，"
        "直接说明 MCP 尚未加载或仍在重连，等工具出现后再调用。在 prepare/reboot 给出唯一下一步之前，"
        "禁止打开 PLAN.md、全仓搜索、project_scan 或大范围 graph 扫描，也不得用 CLI/Python 绕过 MCP 硬门，"
        "不得创建、编辑或删除文件。必须先 prepare（inspect/new/resume）；未 prepare 前其他项目工具返回 PREPARE_REQUIRED。"
        "实施任务同样先 prepare；意图或任务归属有歧义时不按最近时间猜测，include_user 默认 false。\n"
        + STARTUP_FALLBACK +
        CORRECTION_CHECKPOINT +
        "- Devin 不注册 MemWeave 生命周期 Hook；本 always-on Rule 就是会话启动指令通道。"
        "仅出现 memweave_* 工具而没有本 Rule 时，不算完整启动。"
        "Devin 不提供压缩快照，每次新会话和恢复仍通过常驻 MCP 的首轮 workspace_prepare 完成，"
        "不声称已经自动接续。\n"
        "- 同一请求内的非破坏性步骤要连续完成，不因完成一个子步骤或保存检查点就交回控制权，"
        "也不要把中间汇报当成结束本轮的理由。只有请求完成、真实阻塞、必须决策或破坏性操作才返回；"
        "检查点和知识维护应静默执行，不向用户请求例行批准。\n"
        "- 按 .devin/skills/memweave-continuity/SKILL.md 在重要发现、失败、阶段变化和验证后"
        "保存检查点，结束后核对验收并提炼，后续相关工作核对来源后复用；工具分 首轮/续写/少用。\n"
        "- 当前用户要求优先；不保存凭据或完整对话，不把未运行的验证标为通过。"
        "日常维护尽量安静，不要求用户打开管理页。\n"
    ).encode("utf-8")


def _load_global(path):
    if not path.exists():
        return {}, None
    if not path.is_file() or path.stat().st_size > 1048576:
        raise DiagnosticError("CONFIG_INVALID", "Devin mcp_config.json must be a bounded file.")
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise DiagnosticError("CONFIG_INVALID", "Devin mcp_config.json must contain valid UTF-8 JSON.") from error
    if not isinstance(document, dict):
        raise DiagnosticError("CONFIG_INVALID", "Devin mcp_config.json must contain a JSON object.")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise DiagnosticError("CONFIG_INVALID", "Devin mcpServers must be an object.")
    return document, raw


def _safe_global_path(path):
    for candidate in (path, path.parent):
        if candidate.is_symlink() or candidate.is_junction():
            raise DiagnosticError("SETUP_CONFLICT", "Linked Devin configuration paths are not supported.")


def _global_plan(path, server, manifest):
    document, raw = _load_global(path)
    servers = document.setdefault("mcpServers", {})
    existing = servers.get("memweave")
    if existing is not None and existing not in (server, manifest.get("server")):
        raise DiagnosticError(
            "SETUP_CONFLICT",
            "Existing Devin MemWeave MCP differs; preserve or merge it before setup.",
        )
    if existing == server:
        return None
    servers["memweave"] = server
    return json_bytes(document), raw


def _apply_global(path, content, old):
    backup = None
    if old is not None:
        backup = path.parent / "memweave-backups" / uuid4().hex
        replace_file(backup / "mcp_config.json", old)
    replace_file(path, content)
    return str(backup) if backup else None


def manifest_data(project):
    manifest = project.load(MANIFEST)
    if not manifest:
        if project.read(MANIFEST) is not None:
            raise DiagnosticError("CONFIG_INVALID", "Empty Devin installation manifest.")
        return {}
    resources = manifest.get("resources")
    if (
        manifest.get("format") != 1 or manifest.get("client") != "devin"
        or manifest.get("root") != str(project.root)
        or manifest.get("toolset") not in (None, "core", "full", "universal")
        or not isinstance(manifest.get("version"), str)
        or not isinstance(manifest.get("home"), str)
        or not Path(manifest["home"]).is_absolute()
        or not isinstance(manifest.get("server"), dict)
        or not isinstance(manifest.get("mcp_config"), str)
        or not Path(manifest["mcp_config"]).is_absolute()
        or not isinstance(resources, dict) or set(resources) != {SKILL, RULE}
        or any(
            not isinstance(resource, dict) or resource.get("managed") is not True
            or not isinstance(resource.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", resource["sha256"])
            for resource in resources.values()
        )
    ):
        raise DiagnosticError("CONFIG_INVALID", "Invalid Devin installation manifest; preserve it before repairing.")
    return manifest


def devin_ignore_lines():
    return [
        "/.gitignore", "/memweave.json", "/memweave-backups/", "/skills/memweave-continuity/",
        "/rules/memweave.md", "/.memweave-setup.lock",
    ]


def installation_plan(project, home, toolset, manifest, mcp_config):
    server = server_command(home)
    skill = files("memweave").joinpath(
        "skills", "memweave-continuity", "SKILL.md"
    ).read_bytes()
    resources = {SKILL: skill, RULE: rule_text(project.root)}
    ownership = {}
    desired = {}
    for relative, content in resources.items():
        current = project.read(relative)
        old = manifest.get("resources", {}).get(relative, {})
        if current is not None and current != content and fingerprint(current) != old.get("sha256"):
            raise DiagnosticError("SETUP_CONFLICT", f"Resource was modified; merge it before setup: {relative}.")
        desired[relative] = content
        ownership[relative] = {"sha256": fingerprint(content), "managed": True}
    desired[".gitignore"] = merged_ignore(project, ".gitignore", devin_ignore_lines())
    desired[MANIFEST] = json_bytes(dict(
        format=1, client="devin", version=__version__, root=str(project.root),
        home=str(home), toolset="universal", server=server, mcp_config=str(mcp_config),
        resources=ownership,
    ))
    return {
        relative: (project.read(relative), content)
        for relative, content in desired.items() if project.read(relative) != content
    }


def setup_devin(root, home=None, toolset=None, global_base=None, write_global=True):
    project = ProjectFiles(root, directory=".devin")
    selected = selected_home(home, {})
    mcp_config = devin_config_path(global_base)
    _safe_global_path(mcp_config)
    manifest = manifest_data(project)
    selected = selected_home(home, manifest)
    installation_plan(project, selected, toolset, manifest, mcp_config)
    project.cursor.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(project.path(".memweave-setup.lock"), timeout=3):
            manifest = manifest_data(project)
            selected = selected_home(home, manifest)
            plan = installation_plan(project, selected, toolset, manifest, mcp_config)
            global_changed = False
            global_backup = None
            if write_global:
                mcp_config.parent.mkdir(parents=True, exist_ok=True)
                with FileLock(str(mcp_config.parent / ".memweave-mcp-setup.lock"), timeout=3):
                    global_plan = _global_plan(mcp_config, server_command(selected), manifest)
                    if global_plan is not None:
                        content, old = global_plan
                        global_backup = _apply_global(mcp_config, content, old)
                        global_changed = True
            service = Service(selected)
            service.initialize()
            service.project_register(
                str(project.root), project.root.name,
                "setup-" + fingerprint(str(project.root).encode("utf-8")),
            )
            backup = apply_plan(project, plan, backup_prefix="memweave-backups")
        changed = list(plan) + ([str(mcp_config)] if global_changed else [])
        return report(
            project, selected,
            state="installed" if changed else "up_to_date",
            mcp_config_path=str(mcp_config),
            changed_files=changed, backup_directory=backup,
            global_backup_directory=global_backup,
        )
    except Timeout as error:
        raise DiagnosticError("LOCK_BUSY", "Another Devin setup is running; retry after it finishes.") from error


def check_devin(root, home=None, probe=False, global_base=None):
    project = ProjectFiles(root, directory=".devin")
    checks = []
    selected = selected_home(home, {})
    mcp_config = devin_config_path(global_base)

    def check(name, passed, message, fix=None):
        checks.append(dict(
            name=name, state="ok" if passed else "error", message=message,
            fix="" if passed else fix or "重新运行 memweave setup --client devin --root <项目目录>。",
        ))

    try:
        manifest = manifest_data(project)
        selected = selected_home(home, manifest)
        desired = server_command(selected)
        check("installation", bool(manifest), "Devin 项目安装清单存在。")
        try:
            global_document, _ = _load_global(mcp_config)
            global_server = global_document.get("mcpServers", {}).get("memweave")
        except DiagnosticError:
            global_server = None
            check("global_config", False, "Devin 全局 MCP 配置无法解析。", "保留自定义内容后重新运行 setup。")
        else:
            check("global_config", mcp_config.is_file(), "Devin 全局 MCP 配置文件存在。")
        check(
            "mcp_global", global_server == desired,
            "Devin 全局 MCP 条目必须匹配当前解释器、参数和记忆库。",
            "显式运行 memweave setup --client devin；全局配置不会被 clients doctor --fix 自动改写。",
        )
        if not manifest:
            status = Diagnostics(selected).status()["data"]
            check("memory_home", status["initialized"] and status["task_memory"] == "available",
                  "记忆库已经初始化且业务存储可用。")
            output = report(
                project, selected, state="needs_attention", mcp_config_path=str(mcp_config),
                checks=checks,
            )
            return output
        check("version", manifest["version"] == __version__, "接入资源版本与当前安装包一致。")
        check("server_command", manifest["server"] == desired,
              "清单中的服务命令、参数和环境必须匹配当前 MemWeave 安装环境。")
        check(
            "interpreter", isinstance(desired.get("command"), str) and Path(desired["command"]).is_file(),
            "配置中的 Python 解释器路径存在。",
        )
        check("home", manifest["home"] == str(selected), "当前记忆库与安装清单一致。")
        check("mcp_path", manifest["mcp_config"] == str(mcp_config), "安装清单指向当前 Devin 全局 MCP 配置。")
        skill = files("memweave").joinpath(
            "skills", "memweave-continuity", "SKILL.md"
        ).read_bytes()
        rule = rule_text(project.root)
        expected = {SKILL: fingerprint(skill), RULE: fingerprint(rule)}
        manifest_resources_match = all(
            expected.get(relative) == resource["sha256"]
            for relative, resource in manifest["resources"].items()
        )
        check(
            "manifest",
            manifest["version"] == __version__ and manifest["server"] == desired
            and manifest["home"] == str(selected) and manifest["mcp_config"] == str(mcp_config)
            and manifest.get("toolset") == "universal" and manifest_resources_match,
            "安装清单与当前服务、全局 MCP、记忆库和资源一致。",
        )
        for relative, expected_content in ((SKILL, skill), (RULE, rule)):
            check(
                "skill" if relative == SKILL else "rule",
                project.read(relative) == expected_content,
                "资源存在且受管内容与当前包一致。",
                "资源缺失时运行 setup；如有自定义修改，请先备份合并，再运行 setup。",
            )
        ignore_content = project.read(".gitignore")
        try:
            ignore_text = ignore_content.decode("utf-8-sig") if ignore_content is not None else ""
        except UnicodeError:
            ignore_text = ""
        check(
            "gitignore", all(line in set(ignore_text.splitlines()) for line in devin_ignore_lines()),
            "Devin 本机受管文件与备份目录已加入项目内忽略规则。",
        )
        status = Diagnostics(selected).status()["data"]
        check("memory_home", status["initialized"] and status["task_memory"] == "available",
              "记忆库已经初始化且业务存储可用。")
        if status["initialized"] and status["task_memory"] == "available":
            prepared = Service(selected).workspace_prepare(str(project.root), intent="inspect")
            check("project", prepared["state"] == "ready", "项目根目录或同仓库 worktree 已登记。")
    except (DiagnosticError, ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        check("configuration", False, str(error) if isinstance(error, DiagnosticError) else "Invalid configuration.")
        selected = selected_home(home, {})
        global_server = None
    output = report(
        project, selected,
        state="ready" if checks and all(item["state"] == "ok" for item in checks) else "needs_attention",
        mcp_config_path=str(mcp_config), checks=checks,
    )
    if probe and output["state"] == "ready":
        try:
            verified = asyncio.run(probe_installation(server_command(selected), project.root))
            valid = (
                verified["version"] == __version__ and verified["toolset"] == "universal"
                and verified["tool_count"] == output["tool_count"]
                and Path(verified["home"]) == selected
            )
            check("protocol", valid, "已验证 Devin 配置的 stdio 服务版本、工具面、根目录和记忆库。")
            if valid:
                output.update(protocol_verification="sdk_stdio", active_task_count=verified["active_task_count"])
        except Exception:
            check("protocol", False, "配置中的 stdio 服务未完成协议自检。")
        output["state"] = "ready" if all(item["state"] == "ok" for item in checks) else "needs_attention"
    return output
