import json
import os
import shlex
import base64
import sys
from pathlib import Path

from memweave.diagnostics import DiagnosticError


CURSOR_HOOK_EVENTS = ("sessionStart", "sessionEnd")
CODEX_HOOK_EVENTS = (
    "SessionStart", "PreCompact", "UserPromptSubmit", "Interrupt", "SessionEnd",
)
CURSOR_LEGACY_HOOK_EVENTS = (
    "stop", "preCompact", "afterMCPExecution", "afterFileEdit",
)
CODEX_LEGACY_HOOK_EVENTS = (
    "Stop", "UserPromptSubmit", "PostToolUse", "PreCompact",
)


def server_command(home: Path, toolset: str | None = None, *, diagnostic: bool = False) -> dict:
    """Return the one configured MCP entry; toolset is accepted only for legacy callers."""
    if toolset not in (None, "core", "full", "universal"):
        raise DiagnosticError("RECORD_INVALID", "Tool selection is not a user-facing mode.")
    args = ["-m", "memweave", "serve", "--home", str(home.expanduser().resolve())]
    if diagnostic:
        args.append("--diagnostic")
    return {
        "command": str(Path(sys.executable).absolute()),
        "args": args,
        "env": {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
    }


def codex_server_command(home: Path) -> dict:
    return {**server_command(home), "required": True, "startup_readiness": "catalog"}


def client_configuration(client: str, kind: str, home: Path, toolset: str | None = None, *, root=None) -> str:
    server = server_command(home)
    if kind == "hooks":
        if client not in ("cursor", "codex"):
            raise DiagnosticError("RECORD_INVALID", "This client does not use MemWeave lifecycle Hooks.")
        if client == "codex":
            if root is None or not Path(root).is_absolute():
                raise DiagnosticError("RECORD_INVALID", "Codex Hooks require an absolute --root.")
            hooks = {}
            for event in CODEX_HOOK_EVENTS:
                arguments = [
                    server["command"], "-X", "utf8", "-m", "memweave", "hook",
                    "--home", str(home.expanduser().resolve()), "--client", "codex",
                    "--event", event, "--root", str(Path(root).resolve()),
                ]
                script = "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                entry = {
                    "type": "command", "command": shlex.join(arguments),
                    "commandWindows": "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded,
                    "timeout": 3 if event in ("Interrupt", "SessionEnd") else 10,
                }
                if event in ("SessionStart", "UserPromptSubmit"):
                    entry["additionalContextLimit"] = 5000
                group = {"hooks": [entry]}
                if event == "SessionStart":
                    group["matcher"] = "startup|resume|clear|compact|fork"
                hooks[event] = [group]
            return json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2)
        hooks = {}
        for event in CURSOR_HOOK_EVENTS:
            arguments = [
                server["command"], "-X", "utf8", "-m", "memweave", "hook",
                "--home", str(home.expanduser().resolve()),
                "--client", "cursor", "--event", event,
            ]
            if os.name == "nt":
                command = "& " + " ".join(
                    "'" + argument.replace("'", "''") + "'" for argument in arguments
                )
            else:
                command = shlex.join(arguments)
            hooks[event] = [{"command": command}]
        return json.dumps({"version": 1, "hooks": hooks}, ensure_ascii=False, indent=2)
    if client in ("cursor", "devin", "generic"):
        return json.dumps({"mcpServers": {"memweave": server}}, ensure_ascii=False, indent=2)
    return "\n".join([
        "[mcp_servers.memweave]",
        "command = " + json.dumps(server["command"], ensure_ascii=False),
        "args = " + json.dumps(server["args"], ensure_ascii=False),
        'required = true',
        'startup_readiness = "catalog"',
        "",
        "[mcp_servers.memweave.env]",
        'PYTHONUTF8 = "1"',
        'PYTHONUNBUFFERED = "1"',
    ])
