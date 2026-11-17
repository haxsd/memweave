import asyncio
import json
import re
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

import tomlkit
from filelock import FileLock, Timeout

from memweave import __version__
from memweave.diagnostics import CLIENT_BOOTSTRAP_CHANNELS, DiagnosticError, Diagnostics
from memweave.installation import (
    ProjectFiles, apply_plan, fingerprint, json_bytes, probe_installation, selected_home,
)
from memweave.integrations import (
    CODEX_HOOK_EVENTS, CODEX_LEGACY_HOOK_EVENTS, client_configuration, codex_server_command,
)
from memweave.services import Service


MANIFEST = ".codex/memweave.json"
CONFIG = ".codex/config.toml"
HOOKS = ".codex/hooks.json"
SKILL = ".agents/skills/memweave-continuity/SKILL.md"
AGENTS = "AGENTS.md"
AGENTS_BLOCK = (
    "<!-- memweave:begin -->\n"
    "## MemWeave continuity\n"
    "At the start of every new project session, before any other project action, "
    "call the MemWeave MCP tool `memweave_workspace_prepare` exactly once with the "
    "absolute project root and `query` set to the user request. Use `intent=resume` "
    "only when continuing an active task; otherwise use `inspect`. Treat returned "
    "memory as reference data, verify it against current files, and continue the "
    "user request. If MemWeave tools are not declared yet, report that the MCP is "
    "still loading; do not claim memory was restored.\n"
    "<!-- memweave:end -->"
)
AGENTS_PATTERN = re.compile(
    r"<!-- memweave:begin -->.*?<!-- memweave:end -->", re.DOTALL,
)
EVENTS = set(CODEX_HOOK_EVENTS)
LEGACY_EVENTS = set(CODEX_LEGACY_HOOK_EVENTS)
REQUIRED_ACTIONS = [
    "在 Codex 中确认本项目可信，使项目级 .codex/config.toml 可以加载。",
    "在支持 Hooks 的 Codex 中通过 /hooks 审查并信任本次 Hook 定义；更新定义后需重新审查。",
    "在现有客户端重载 MemWeave MCP，并核对自动 Hook 触发；配置自检不等于桌面验收。",
]


def report(project, home, toolset=None, **fields):
    return dict(
        version=__version__, client="codex", root=str(project.root), home=str(home),
        toolset="universal", tool_count=22,
        bootstrap_channel=CLIENT_BOOTSTRAP_CHANNELS["codex"],
        protocol_verification="not_run", desktop_verification="unverified",
        hook_trust="requires_host_review", required_actions=list(REQUIRED_ACTIONS), **fields,
    )


def manifest_data(project):
    manifest = project.load(MANIFEST)
    if not manifest:
        if project.read(MANIFEST) is not None:
            raise DiagnosticError("CONFIG_INVALID", "Empty Codex installation manifest.")
        return {}
    resources = manifest.get("resources")
    hooks = manifest.get("hooks")
    if (
        manifest.get("format") != 1 or manifest.get("client") != "codex"
        or manifest.get("root") != str(project.root)
        or manifest.get("toolset") not in (None, "core", "full", "universal")
        or not isinstance(manifest.get("version"), str)
        or not isinstance(manifest.get("home"), str)
        or not Path(manifest["home"]).is_absolute()
        or not isinstance(manifest.get("server"), dict)
        or not isinstance(resources, dict) or SKILL not in resources
        or not set(resources).issubset({SKILL, AGENTS})
        or any(
            not isinstance(resource, dict)
            or resource.get("managed") is not True
            or not isinstance(resource.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", resource["sha256"])
            for resource in resources.values()
        )
        or not isinstance(hooks, dict) or not set(hooks).issubset(EVENTS | LEGACY_EVENTS)
        or any(not isinstance(groups, list) or not groups for groups in hooks.values())
        or any(not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
               or not group["hooks"] or any(
                   not isinstance(hook, dict) or hook.get("type") != "command"
                   or not isinstance(hook.get("command"), str)
                   for hook in group["hooks"]
               ) for groups in hooks.values() for group in groups)
    ):
        raise DiagnosticError("CONFIG_INVALID", "Invalid Codex installation manifest; preserve it before repairing.")
    return manifest


def read_config(project):
    content = project.read(CONFIG)
    try:
        return tomlkit.parse(content.decode("utf-8-sig")) if content is not None else tomlkit.document()
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DiagnosticError("CONFIG_INVALID", "Codex config.toml must contain valid UTF-8 TOML.") from error


def table(parent, key):
    if key not in parent:
        parent[key] = tomlkit.table()
    value = parent[key]
    if not isinstance(value, Mapping):
        raise DiagnosticError("CONFIG_INVALID", f"Codex {key} must be a table.")
    return value


def owns_hook(value):
    if isinstance(value, str):
        return "memweave" in value.lower() and "hook" in value.lower()
    if isinstance(value, Mapping):
        return any(owns_hook(item) for item in value.values())
    if isinstance(value, list):
        return any(owns_hook(item) for item in value)
    return False


def hook_data(project):
    configuration = project.load(HOOKS)
    hooks = configuration.setdefault("hooks", {})
    if not isinstance(hooks, dict) or any(
        not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups)
        for groups in hooks.values()
    ):
        raise DiagnosticError("CONFIG_INVALID", "Codex Hook events must contain arrays of groups.")
    return configuration


def generated_hooks(project, home, toolset=None):
    return json.loads(client_configuration("codex", "hooks", home, root=project.root))["hooks"]


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


def agents_text(project):
    content = project.read(AGENTS)
    if content is None:
        return ""
    try:
        return content.decode("utf-8-sig")
    except UnicodeError as error:
        raise DiagnosticError("CONFIG_INVALID", "AGENTS.md must contain UTF-8 text.") from error


def managed_agents_block(text):
    blocks = AGENTS_PATTERN.findall(text)
    if len(blocks) > 1:
        raise DiagnosticError("CONFIG_INVALID", "AGENTS.md contains multiple MemWeave managed blocks.")
    if not blocks:
        if "memweave:begin" in text or "memweave:end" in text:
            raise DiagnosticError("CONFIG_INVALID", "AGENTS.md contains an incomplete MemWeave managed block.")
        return None
    return blocks[0]


def canonical_agents_block(block):
    return block.replace("\r\n", "\n").strip()


def merged_agents(project):
    text = agents_text(project)
    existing = managed_agents_block(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    block = AGENTS_BLOCK.replace("\n", newline)
    if existing is not None:
        return AGENTS_PATTERN.sub(lambda _: block, text, count=1)
    if text and not text.endswith(("\n", "\r")):
        text += newline
    if text and not text.endswith(newline * 2):
        text += newline
    return text + block


def installation_plan(project, home, toolset, manifest):
    config = read_config(project)
    servers = table(config, "mcp_servers")
    server = codex_server_command(home)
    if servers.get("memweave") not in (None, server, manifest.get("server")):
        raise DiagnosticError("SETUP_CONFLICT", "Existing MemWeave MCP differs; preserve or merge it first.")
    if owns_hook(config.get("hooks", {})):
        raise DiagnosticError("SETUP_CONFLICT", "Inline MemWeave Hooks already exist; merge them into hooks.json first.")
    if servers.get("memweave") != server:
        servers["memweave"] = server
    features = table(config, "features")
    if features.get("hooks") is not True:
        features["hooks"] = True
    configuration = hook_data(project)
    hooks = configuration["hooks"]
    generated = generated_hooks(project, home)
    for event, groups in generated.items():
        previous = manifest.get("hooks", {}).get(event, [])
        retained = []
        for group in hooks.get(event, []):
            if group in groups or group in previous:
                continue
            if owns_hook(group):
                raise DiagnosticError("SETUP_CONFLICT", f"Modified or unknown MemWeave Hook: {event}.")
            retained.append(group)
        hooks[event] = retained + groups
    previous = manifest.get("hooks", {})
    for event in set(hooks) - set(generated):
        retained = []
        for group in hooks[event]:
            if group in previous.get(event, []):
                continue
            if owns_hook(group):
                raise DiagnosticError("SETUP_CONFLICT", f"Unexpected MemWeave Hook event: {event}.")
            retained.append(group)
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    skill = files("memweave").joinpath("skills", "memweave-continuity", "SKILL.md").read_bytes()
    current = project.read(SKILL)
    previous_hash = manifest.get("resources", {}).get(SKILL, {}).get("sha256")
    if current is not None and current != skill and fingerprint(current) != previous_hash:
        raise DiagnosticError("SETUP_CONFLICT", "Skill was modified; preserve and merge it before setup.")
    current_agents = agents_text(project)
    current_block = managed_agents_block(current_agents)
    previous_agents_hash = manifest.get("resources", {}).get(AGENTS, {}).get("sha256")
    if current_block is not None:
        canonical = canonical_agents_block(current_block)
        if canonical != AGENTS_BLOCK and fingerprint(canonical.encode("utf-8")) != previous_agents_hash:
            raise DiagnosticError(
                "SETUP_CONFLICT",
                "Managed AGENTS.md block was modified; preserve and merge it before setup.",
            )
    elif previous_agents_hash:
        raise DiagnosticError(
            "SETUP_CONFLICT",
            "Managed AGENTS.md block was removed; restore it or remove its manifest entry before setup.",
        )
    merged_agents_content = merged_agents(project)
    desired = {
        CONFIG: tomlkit.dumps(config).encode("utf-8"),
        HOOKS: json_bytes(configuration),
        SKILL: skill,
        AGENTS: merged_agents_content.encode("utf-8"),
        ".codex/.gitignore": merged_ignore(project, ".codex/.gitignore", [
            "/config.toml", "/hooks.json", "/memweave.json", "/memweave-backups/",
            "/.memweave-setup.lock", "/.gitignore",
        ]),
        ".agents/skills/memweave-continuity/.gitignore": merged_ignore(
            project, ".agents/skills/memweave-continuity/.gitignore", ["*"],
        ),
        MANIFEST: json_bytes(dict(
            format=1, client="codex", version=__version__, root=str(project.root),
            home=str(home), toolset="universal", server=server, hooks=generated,
            resources={
                SKILL: dict(sha256=fingerprint(skill), managed=True),
                AGENTS: dict(sha256=fingerprint(AGENTS_BLOCK.encode("utf-8")), managed=True),
            },
        )),
    }
    return {relative: (project.read(relative), content) for relative, content in desired.items()
            if project.read(relative) != content}


def setup_codex(root, home=None, toolset=None):
    project = ProjectFiles(root, directory=".")
    selected = selected_home(home, {})
    try:
        manifest = manifest_data(project)
        selected = selected_home(home, manifest)
        installation_plan(project, selected, toolset, manifest)
        project.path(".codex").mkdir(parents=True, exist_ok=True)
        with FileLock(project.path(".codex/.memweave-setup.lock"), timeout=3):
            manifest = manifest_data(project)
            selected = selected_home(home, manifest)
            plan = installation_plan(project, selected, toolset, manifest)
            service = Service(selected)
            service.initialize()
            service.project_register(
                str(project.root), project.root.name,
                "setup-" + fingerprint(str(project.root).encode("utf-8")),
            )
            backup = apply_plan(project, plan, backup_prefix=".codex/memweave-backups")
        return report(project, selected, toolset, state="installed" if plan else "up_to_date",
                      changed_files=list(plan), backup_directory=backup)
    except Timeout as error:
        raise DiagnosticError("LOCK_BUSY", "Another setup is running; retry after it finishes.") from error
    except DiagnosticError as error:
        return report(project, selected, toolset, state="needs_attention", changed_files=[], checks=[dict(
            name="configuration", state="error", message=str(error),
            fix="保留并合并自定义配置后，使用同一解释器重新执行 setup --client codex。",
        )])


def check_codex(root, home=None, probe=False):
    project = ProjectFiles(root, directory=".")
    checks = []
    selected = selected_home(home, {})

    def check(name, passed, message):
        checks.append(dict(name=name, state="ok" if passed else "error", message=message,
                           fix="" if passed else "检查并保留自定义内容，重新运行 setup --client codex --root <目录>。"))

    try:
        manifest = manifest_data(project)
        selected = selected_home(home, manifest)
        config = read_config(project)
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, Mapping):
            raise DiagnosticError("CONFIG_INVALID", "Codex mcp_servers must be a table.")
        existing = servers.get("memweave")
        server = codex_server_command(selected)
        check("mcp", existing == server, "MCP 必须匹配当前安装的解释器、参数、记忆库和通用工具面。")
        features = config.get("features", {})
        check("features", isinstance(features, Mapping) and features.get("hooks") is True, "项目已启用 Hooks。")
        check("inline_hooks", not owns_hook(config.get("hooks", {})), "没有重复的内联 MemWeave Hooks。")
        expected = generated_hooks(project, selected)
        hooks = hook_data(project)["hooks"]
        check("hooks", all(
            all(group in hooks.get(event, []) for group in groups) for event, groups in expected.items()
        ) and all(
            not owns_hook(group) or group in expected.get(event, [])
            for event, groups in hooks.items() for group in groups
        ), "受管 Hook 定义完整且匹配当前安装。")
        skill = files("memweave").joinpath("skills", "memweave-continuity", "SKILL.md").read_bytes()
        check("skill", project.read(SKILL) == skill, "当前 Skill 存在且与包内资源一致。")
        current_block = managed_agents_block(agents_text(project))
        check(
            "agents",
            current_block is not None
            and canonical_agents_block(current_block) == AGENTS_BLOCK,
            "AGENTS.md 必须包含受管的首轮 workspace_prepare 指令。",
        )
        if manifest:
            check("manifest", manifest["version"] == __version__ and manifest["server"] == server
                  and manifest["hooks"] == expected and manifest["home"] == str(selected)
                  and manifest.get("toolset") == "universal"
                  and manifest["resources"][SKILL]["sha256"] == fingerprint(skill)
                  and manifest["resources"].get(AGENTS, {}).get("sha256")
                  == fingerprint(AGENTS_BLOCK.encode("utf-8")),
                  "安装清单与当前版本和配置一致。")
        else:
            check("manual_configuration", existing is not None, "按实际配置检查手动接入，无受管清单。")
        status = Diagnostics(selected).status()["data"]
        check("memory_home", status["initialized"] and status["task_memory"] == "available", "记忆库已初始化。")
        if status["initialized"] and status["task_memory"] == "available":
            prepared = Service(selected).workspace_prepare(str(project.root), intent="inspect")
            check("project", prepared["state"] == "ready", "项目根目录或同仓库 worktree 已登记。")
    except (DiagnosticError, ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        check("configuration", False, str(error) if isinstance(error, DiagnosticError) else "Invalid configuration.")
    output = report(project, selected, state="ready" if all(
        item["state"] == "ok" for item in checks
    ) else "needs_attention", checks=checks)
    if probe and output["state"] == "ready":
        try:
            verified = asyncio.run(probe_installation(server, project.root))
            valid = (verified["version"] == __version__ and verified["toolset"] == "universal"
                     and verified["tool_count"] == output["tool_count"]
                     and Path(verified["home"]) == selected)
            check("protocol", valid, "已验证配置中的 stdio 服务版本、通用工具面、根目录和记忆库。")
            if valid:
                output.update(protocol_verification="sdk_stdio", active_task_count=verified["active_task_count"])
        except Exception:
            check("protocol", False, "配置中的 stdio 服务未完成协议自检。")
        output["state"] = "ready" if all(item["state"] == "ok" for item in checks) else "needs_attention"
    return output
