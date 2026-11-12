import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import tomlkit

from memweave.codex_installation import check_codex, setup_codex
from memweave.devin_installation import (
    MANIFEST as DEVIN_MANIFEST, RULE as DEVIN_RULE, SKILL as DEVIN_SKILL,
    check_devin, devin_config_path, setup_devin,
)
from memweave.diagnostics import DiagnosticError, Diagnostics
from memweave.installation import ProjectFiles, check_installation, load_manifest, setup_cursor
from memweave.storage import Vault
from memweave.worktrees import linked_worktrees, same_repository


CLIENTS = ("cursor", "codex", "devin")
PROJECT_MANIFEST = {
    "cursor": "memweave.json", "codex": ".codex/memweave.json",
    "devin": ".devin/memweave.json",
}
PROJECT_CONFIG = {
    "cursor": "mcp.json", "codex": ".codex/config.toml",
    "devin": ".devin/memweave.json",
}
CHECK_ISSUES = {
    "version": ("version_mismatch", "安装清单版本落后于当前包。", True),
    "server_command": ("server_mismatch", "服务命令或参数与清单不一致。", True),
    "interpreter": ("interpreter_missing", "清单中的 Python 解释器不存在。", True),
    "mcp": ("mcp_mismatch", "项目 MCP 配置与安装清单不一致。", True),
    "features": ("hooks_disabled", "项目尚未启用 Codex Hooks。", True),
    "hooks": ("hooks_missing", "受管 Hook 定义缺失或已变化。", True),
    "inline_hooks": ("duplicate_hooks", "存在重复的内联 Hook 定义，需人工合并。", False),
    "skill": ("skill_drift", "受管 Skill 缺失、落后于当前包或已被人工修改。", True),
    "rule": ("rule_drift", "受管 Cursor Rule 缺失、落后于当前包或已被人工修改。", True),
    "gitignore": ("local_ignore_missing", "Cursor 本机受管文件尚未被项目内忽略规则覆盖。", True),
    "manifest": ("manifest_drift", "安装清单与当前服务或资源不一致。", True),
    "memory_home": ("memory_home_unavailable", "清单指向的记忆库不可用。", True),
    "project": ("project_unregistered", "项目根目录或同仓库 worktree 尚未登记到活动记忆库。", True),
    "configuration": ("config_invalid", "客户端配置无法安全解析或合并。", False),
    "protocol": ("protocol_failed", "协议自检失败。", False),
}


def _normal_path(value, label):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DiagnosticError("RECORD_INVALID", f"{label} must be an absolute path.")
    return path.resolve()


def _key(path):
    return str(Path(path).resolve()).casefold()


def _registered_project(root, registered):
    path = Path(root).resolve()
    if _key(path) in registered:
        return True
    return any(same_repository(path, Path(value)) for value in registered)


def _add_issue(issues, code, message, fixable):
    if not any(item["code"] == code for item in issues):
        issues.append({"code": code, "message": message, "fixable": bool(fixable)})


def _server_home(server):
    if not isinstance(server, Mapping):
        return None
    args = server.get("args")
    if not isinstance(args, list) or "--home" not in args:
        return None
    index = args.index("--home")
    if index + 1 >= len(args) or not isinstance(args[index + 1], str):
        return None
    return Path(args[index + 1]).expanduser().resolve()


def _server_toolset(server):
    return "universal"


def _registered_roots(home):
    return [
        str(_normal_path(record["root"], "Registered project root"))
        for record in Vault(home).list_records("project")
    ]


def _codex_global_info(global_base):
    path = global_base / ".codex" / "config.toml"
    info = {"path": path, "server": None, "present": False, "roots": [], "error": None}
    if not path.is_file():
        return info
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
        servers = document.get("mcp_servers", {})
        if isinstance(servers, Mapping):
            server = servers.get("memweave")
            info["present"] = "memweave" in servers
            if info["present"] and not isinstance(server, Mapping):
                raise ValueError("mcp_servers.memweave must be an object")
            info["server"] = dict(server) if isinstance(server, Mapping) else None
        elif "mcp_servers" in document:
            raise ValueError("mcp_servers must be a table")
        projects = document.get("projects", {})
        if isinstance(projects, Mapping):
            for root in projects:
                candidate = Path(str(root)).expanduser()
                if candidate.is_absolute() and candidate.is_dir():
                    info["roots"].append(candidate.resolve())
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        info["error"] = str(error)
    return info


def _cursor_global_info(global_base):
    path = global_base / ".cursor" / "mcp.json"
    info = {"path": path, "server": None, "present": False, "roots": [], "error": None}
    if not path.is_file():
        return info
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            raise ValueError("mcp_config.json must contain an object")
        servers = document.get("mcpServers", {}) if isinstance(document, dict) else {}
        server = servers.get("memweave") if isinstance(servers, dict) else None
        info["present"] = isinstance(servers, dict) and "memweave" in servers
        if info["present"] and not isinstance(server, Mapping):
            raise ValueError("mcpServers.memweave must be an object")
        info["server"] = dict(server) if isinstance(server, Mapping) else None
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        info["error"] = str(error)
    return info


def _global_target(client, info, expected_home, required=False):
    if info["error"]:
        return dict(
            client=client, scope="global", root=None, config_path=str(info["path"]),
            state="needs_attention", home=None, toolset=None, fixable=False,
            issues=[{
                "code": "config_invalid",
                "message": "全局客户端配置无法安全解析。",
                "fixable": False,
            }],
            required_actions=["保留自定义内容并修复全局配置；本命令不会擅自改写全局设置。"],
        )
    if not info["present"]:
        if not required:
            return None
        return dict(
            client=client, scope="global", root=None, config_path=str(info["path"]),
            state="not_installed", home=str(expected_home), toolset=None, fixable=False,
            source="global",
            issues=[{
                "code": "client_not_installed",
                "message": "Devin 缺少全局 MemWeave MCP 配置。",
                "fixable": False,
            }],
            required_actions=["显式运行 memweave setup --client devin；clients doctor --fix 不会改写全局配置。"],
        )
    server = info["server"]
    home = _server_home(server)
    issues = []
    command = server.get("command")
    if not isinstance(command, str) or not Path(command).is_file():
        _add_issue(issues, "interpreter_missing", "全局 MemWeave 入口的解释器不存在。", False)
    if home is None:
        _add_issue(issues, "home_missing", "全局 MemWeave 入口没有有效的 --home 参数。", False)
    elif home != expected_home:
        _add_issue(
            issues, "stale_home",
            f"全局入口使用 {home}，活动记忆库是 {expected_home}。", False,
        )
    return dict(
        client=client, scope="global", root=None, config_path=str(info["path"]),
        state="ready" if not issues else "needs_attention", home=str(home) if home else None,
        toolset=_server_toolset(server), fixable=False, issues=issues,
        required_actions=[] if not issues else ["人工合并或移除本机全局重复入口，然后重启客户端。"],
    )


def _invalid_root_target(client, root, source, message):
    relative = {
        "cursor": ".cursor/mcp.json", "codex": ".codex/config.toml",
        "devin": ".devin/memweave.json",
    }[client]
    return dict(
        client=client, scope="project", source=source, root=str(root),
        config_path=str(Path(root) / relative), state="needs_attention",
        home=None, toolset=None, fixable=False,
        issues=[{"code": "root_invalid", "message": message, "fixable": False}],
    )


def _project_server(root, client):
    project = ProjectFiles(root, directory="." if client == "codex" else ".cursor")
    if client == "cursor":
        mcp = project.load("mcp.json")
        servers = mcp.get("mcpServers", {})
        return servers.get("memweave") if isinstance(servers, Mapping) else None
    content = project.read(PROJECT_CONFIG[client])
    if content is None:
        return None
    document = tomlkit.parse(content.decode("utf-8-sig"))
    servers = document.get("mcp_servers", {})
    return dict(servers.get("memweave")) if isinstance(servers, Mapping) and isinstance(
        servers.get("memweave"), Mapping
    ) else None


def _same_server(left, right, client):
    if client != "codex":
        return left == right
    policy = {"required", "startup_readiness"}
    return {key: value for key, value in left.items() if key not in policy} == {
        key: value for key, value in right.items() if key not in policy
    }


def _devin_global_info(global_base):
    path = devin_config_path(global_base)
    info = {"path": path, "server": None, "present": False, "roots": [], "error": None}
    if not path.is_file():
        return info
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        servers = document.get("mcpServers", {}) if isinstance(document, dict) else {}
        if not isinstance(servers, Mapping):
            raise ValueError("mcpServers must be an object")
        info["present"] = "memweave" in servers
        server = servers.get("memweave")
        if info["present"] and not isinstance(server, Mapping):
            raise ValueError("mcpServers.memweave must be an object")
        info["server"] = dict(server) if isinstance(server, Mapping) else None
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        info["error"] = str(error)
    return info


DEVIN_ISSUES = {
    "version": ("version_mismatch", "安装清单版本落后于当前包。", True),
    "server_command": ("server_mismatch", "清单中的服务命令或参数与当前环境不一致。", True),
    "interpreter": ("interpreter_missing", "清单中的 Python 解释器不存在。", True),
    "home": ("stale_home", "安装清单使用的记忆库与活动记忆库不一致。", True),
    "global_config": ("global_config_invalid", "Devin 全局 MCP 配置不存在或无法解析。", False),
    "mcp_global": ("mcp_global_mismatch", "Devin 全局 MCP 条目与清单不一致。", False),
    "mcp_path": ("manifest_drift", "安装清单指向的 Devin 配置路径已变化。", True),
    "manifest": ("manifest_drift", "安装清单与当前服务或资源不一致。", True),
    "skill": ("skill_drift", "受管 Skill 缺失、落后于当前包或已被人工修改。", True),
    "rule": ("rule_drift", "受管 Rule 缺失、落后于当前包或已被人工修改。", True),
    "gitignore": ("local_ignore_missing", "Devin 本机受管文件尚未被项目内忽略规则覆盖。", True),
    "project": ("project_unregistered", "项目根目录或同仓库 worktree 尚未登记到活动记忆库。", True),
    "configuration": ("config_invalid", "Devin 项目配置无法安全解析或合并。", False),
    "protocol": ("protocol_failed", "协议自检失败。", False),
}


def _inspect_devin_project(root, expected_home, source, global_base, global_server):
    project = ProjectFiles(root, directory=".devin")
    manifest_bytes = project.read(DEVIN_MANIFEST)
    has_resource = any(
        project.read(relative) is not None for relative in (DEVIN_SKILL, DEVIN_RULE)
    )
    if manifest_bytes is None and not has_resource:
        if source == "codex_known":
            return None
        fixable = source != "explicit"
        return dict(
            client="devin", scope="project", source=source, root=str(project.root),
            config_path=str(project.path(DEVIN_MANIFEST)), state="not_installed",
            home=str(expected_home), toolset=None, fixable=fixable,
            issues=[] if not fixable else [{
                "code": "client_not_installed",
                "message": "已发现的项目根缺少 Devin 项目级 Skill 与 Rule。",
                "fixable": True,
            }],
        )
    checked = check_devin(
        project.root, expected_home, probe=False, global_base=global_base,
    )
    issues = []
    checks = []
    for item in checked.get("checks", []):
        if item.get("state") == "ok":
            continue
        checks.append(dict(name=item["name"], message=item["message"]))
        mapped = DEVIN_ISSUES.get(item["name"])
        if mapped:
            _add_issue(issues, *mapped)
    if manifest_bytes is None:
        _add_issue(issues, "unmanaged_installation", "存在 Devin 项目资源，但没有受管安装清单。", True)
    fixable = any(item["fixable"] for item in issues)
    return dict(
        client="devin", scope="project", source=source, root=str(project.root),
        config_path=str(project.path(DEVIN_MANIFEST)),
        state="ready" if not issues else "needs_attention",
        home=str(expected_home), toolset="universal" if manifest_bytes is not None else None,
        fixable=fixable, issues=issues, checks=checks,
    )


def _inspect_project(root, client, expected_home, source, registered, global_server, global_base):
    if client == "devin":
        return _inspect_devin_project(
            root, expected_home, source, global_base, global_server,
        )
    project = ProjectFiles(root, directory="." if client == "codex" else ".cursor")
    manifest_bytes = project.read(PROJECT_MANIFEST[client])
    try:
        server = _project_server(root, client)
    except (DiagnosticError, UnicodeError, ValueError, TypeError) as error:
        return dict(
            client=client, scope="project", source=source, root=str(project.root),
            config_path=str(project.path(PROJECT_CONFIG[client])), state="needs_attention",
            home=None, toolset=None, fixable=False,
            issues=[{
                "code": "config_invalid",
                "message": error.code if isinstance(error, DiagnosticError) else "项目配置无法解析。",
                "fixable": False,
            }],
        )
    if manifest_bytes is None and server is None:
        if source == "worktree":
            return dict(
                client=client, scope="project", source=source, root=str(project.root),
                config_path=str(project.path(PROJECT_CONFIG[client])), state="needs_attention",
                home=str(expected_home), toolset=None, fixable=True,
                issues=[{
                    "code": "worktree_unconfigured",
                    "message": "该 linked worktree 缺少项目级 MemWeave 安装，无法保证 MCP、Skill 与 Hook 在当前目录生效。",
                    "fixable": True,
                }],
            )
        if source == "codex_known":
            return None
        fixable = source != "explicit"
        return dict(
            client=client, scope="project", source=source, root=str(project.root),
            config_path=str(project.path(PROJECT_CONFIG[client])), state="not_installed",
            home=str(expected_home), toolset=None, fixable=fixable,
            issues=[] if not fixable else [{
                "code": "client_not_installed",
                "message": f"已发现的项目根缺少 {client} 项目级 MemWeave 入口。",
                "fixable": True,
            }],
        )

    issues = []
    checks = []
    manifest = None
    if manifest_bytes is not None:
        try:
            if client == "cursor":
                manifest = load_manifest(project)
            else:
                from memweave.codex_installation import manifest_data
                manifest = manifest_data(project)
        except (DiagnosticError, KeyError, TypeError, AttributeError):
            _add_issue(issues, "config_invalid", "安装清单无法安全解析，修复前需人工保留。", False)

    if manifest is not None:
        manifest_home = Path(manifest["home"]).expanduser().resolve()
        if manifest_home != expected_home:
            _add_issue(
                issues, "stale_home",
                f"项目清单使用 {manifest_home}，活动记忆库是 {expected_home}。", True,
            )
        if not _registered_project(project.root, registered):
            _add_issue(issues, "project_unregistered", "项目根目录尚未登记到活动记忆库。", True)
        checked = check_codex(project.root) if client == "codex" else check_installation(project.root)
        checks = [
            dict(name=item["name"], message=item["message"])
            for item in checked.get("checks", []) if item["state"] != "ok"
        ]
        for item in checked.get("checks", []):
            if item["state"] == "ok":
                continue
            mapped = CHECK_ISSUES.get(item["name"])
            if mapped:
                _add_issue(issues, *mapped)
        toolset = "universal"
        manifest_home_value = str(manifest_home)
    elif manifest_bytes is None:
        _add_issue(issues, "unmanaged_installation", "存在 MemWeave 入口，但没有受管安装清单。", True)
        manifest_home_value = None
        toolset = _server_toolset(server)
    else:
        manifest_home_value = None
        toolset = _server_toolset(server)

    if global_server is not None and server is not None and not _same_server(global_server, server, client):
        _add_issue(
            issues, "duplicate_mcp",
            "本机全局入口与项目入口指向不同服务，客户端可能发生遮蔽。", False,
        )
    if manifest is not None and global_server is not None and not _same_server(global_server, manifest["server"], client):
        _add_issue(
            issues, "duplicate_mcp",
            "本机全局入口与项目安装清单指向不同服务。", False,
        )
    return dict(
        client=client, scope="project", source=source, root=str(project.root),
        config_path=str(project.path(PROJECT_CONFIG[client])),
        state="ready" if not issues else "needs_attention",
        home=manifest_home_value or (str(_server_home(server)) if _server_home(server) else None),
        toolset=toolset, fixable=any(item["fixable"] for item in issues),
        issues=issues, checks=checks,
    )


def _repair(target, home, registered, global_server, global_base):
    if target["scope"] != "project" or not target.get("fixable"):
        return target
    root = Path(target["root"])
    toolset = "universal"
    try:
        if target["client"] == "cursor":
            repaired = setup_cursor(root, home, toolset)
        elif target["client"] == "devin":
            repaired = setup_devin(
                root, home, toolset, global_base=global_base, write_global=False,
            )
        else:
            repaired = setup_codex(root, home, toolset)
        registered_after = {_key(path) for path in _registered_roots(home)}
        refreshed = _inspect_project(
            root, target["client"], home, target["source"], registered_after, global_server,
            global_base,
        ) or target
        refreshed["repair"] = {
            "state": repaired.get("state"),
            "changed_files": repaired.get("changed_files", []),
            "backup_directory": repaired.get("backup_directory"),
            "message": "; ".join(
                item["message"] for item in repaired.get("checks", []) if item.get("state") == "error"
            ),
        }
        if repaired.get("state") == "installed" and refreshed["state"] == "ready":
            refreshed["state"] = "repaired"
        refreshed["restart_required"] = bool(repaired.get("changed_files"))
        return refreshed
    except (DiagnosticError, OSError) as error:
        message = str(error)[:500]
        target["repair"] = {
            "state": "failed", "changed_files": [], "backup_directory": None,
            "message": message, "error_code": "FIX_FAILED",
            "error_type": type(error).__name__,
        }
        _add_issue(target.setdefault("issues", []), "fix_failed", message, False)
        return target


def doctor_clients(home, roots=None, clients=CLIENTS, fix=False, global_base=None):
    home = _normal_path(home, "Memory home")
    status = Diagnostics(home).status()["data"]
    if not status["initialized"] or status["task_memory"] != "available":
        raise DiagnosticError("NOT_INITIALIZED", "Initialize the active memory home before client diagnosis.")
    if not isinstance(clients, (list, tuple)) or not clients or any(
        client not in CLIENTS for client in clients
    ):
        raise DiagnosticError("RECORD_INVALID", "Select at least one supported client.")

    explicit = [_normal_path(root, "Project root") for root in (roots or [])]
    registered_roots = _registered_roots(home)
    registered = {_key(root) for root in registered_roots}
    global_base = _normal_path(global_base or Path.home(), "Global client base")

    infos = {}
    for client in CLIENTS:
        if client == "codex":
            infos[client] = _codex_global_info(global_base)
        elif client == "devin":
            infos[client] = _devin_global_info(global_base)
        else:
            infos[client] = _cursor_global_info(global_base)

    root_sources = {}
    for root in registered_roots:
        root_sources.setdefault(_key(root), (root, "registered"))
    for root in infos["codex"]["roots"]:
        root_sources.setdefault(_key(root), (str(root), "codex_known"))
    for root in explicit:
        root_sources[_key(root)] = (str(root), "explicit")
    for root, _ in list(root_sources.values()):
        for worktree in linked_worktrees(Path(root)):
            root_sources.setdefault(_key(worktree), (str(worktree), "worktree"))

    targets = []
    for client in clients:
        global_target = _global_target(
            client, infos[client], home, required=client == "devin",
        )
        if global_target is not None:
            targets.append(global_target)
        for _, (root, source) in sorted(root_sources.items()):
            try:
                target = _inspect_project(
                    root, client, home, source, registered, infos[client]["server"],
                    global_base,
                )
            except DiagnosticError as error:
                if source == "explicit":
                    raise
                target = _invalid_root_target(client, root, source, str(error))
            if target is not None:
                targets.append(target)

    if fix:
        targets = [
            _repair(
                target, home, registered, infos[target["client"]]["server"], global_base,
            )
            if target["scope"] == "project" else target
            for target in targets
        ]

    counts = Counter(target["state"] for target in targets)
    missing = [
        {
            "client": target["client"],
            "scope": target["scope"],
            "root": target["root"],
            "source": target["source"],
            "global_fallback": infos[target["client"]]["server"] is not None,
            "fixable": bool(target.get("fixable")),
        }
        for target in targets
        if target["state"] == "not_installed"
    ]
    coverage = {
        "state": "complete" if not missing else "incomplete",
        "missing_count": len(missing),
        "missing": missing,
    }
    hard_gap = any(
        item["source"] != "explicit" and not item["global_fallback"]
        for item in missing
    )
    state = (
        "needs_attention" if counts["needs_attention"] or hard_gap
        else "repaired" if counts["repaired"]
        else "ready"
    )
    return {
        "state": state,
        "version": status["version"],
        "home": str(home),
        "roots_scanned": len(root_sources),
        "targets_scanned": len(targets),
        "summary": {
            key: counts.get(key, 0)
            for key in ("ready", "repaired", "needs_attention", "not_installed")
        },
        "coverage": coverage,
        "targets": targets,
    }
