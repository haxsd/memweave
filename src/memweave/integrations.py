import json
import os
import shlex
import base64
import sys
from pathlib import Path

from memweave.diagnostics import DiagnosticError


def server_command(home: Path, toolset: str = "core") -> dict:
    if toolset not in ("core", "full"):
        raise DiagnosticError("RECORD_INVALID", "toolset must be core or full.")
    return {
        "command": str(Path(sys.executable).absolute()),
        "args": ["-m", "memweave", "serve", "--home", str(home.expanduser().resolve())]
                + (["--toolset", "full"] if toolset == "full" else []),
        "env": {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
    }


def client_configuration(client: str, kind: str, home: Path, toolset: str = "core", *, root=None) -> str:
    server = server_command(home, toolset)
    if kind == "hooks":
        if client == "codex":
            if root is None or not Path(root).is_absolute():
                raise DiagnosticError("RECORD_INVALID", "Codex Hooks require an absolute --root.")
            hooks = {}
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact",
                          "Stop", "Interrupt", "SessionEnd"):
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
                if event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
                    entry["additionalContextLimit"] = 5000
                group = {"hooks": [entry]}
                if event == "SessionStart":
                    group["matcher"] = "startup|resume|clear|compact|fork"
                hooks[event] = [group]
            return json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2)
        hooks = {}
        for event in ("sessionStart", "sessionEnd", "preCompact", "afterMCPExecution", "afterFileEdit", "stop"):
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
            if event == "stop":
                hooks[event][0]["loop_limit"] = 1
        return json.dumps({"version": 1, "hooks": hooks}, ensure_ascii=False, indent=2)
    if client == "cursor":
        return json.dumps({"mcpServers": {"memweave": server}}, ensure_ascii=False, indent=2)
    return "\n".join([
        "[mcp_servers.memweave]",
        "command = " + json.dumps(server["command"], ensure_ascii=False),
        "args = " + json.dumps(server["args"], ensure_ascii=False),
        "",
        "[mcp_servers.memweave.env]",
        'PYTHONUTF8 = "1"',
        'PYTHONUNBUFFERED = "1"',
    ])
