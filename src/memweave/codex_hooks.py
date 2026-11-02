import json
import re
import shlex
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.hooks import CursorHooks
from memweave.worktrees import repository_root, same_repository


class CodexHooks(CursorHooks):
    def __init__(self, diagnostics, root, service=None):
        super().__init__(diagnostics, service=service)
        self.root = Path(root).expanduser().resolve() if root is not None else None

    def context(self, event, text):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:5000]}}

    def handle(self, event, payload):
        if self.root is None or not self.root.is_dir():
            return {}
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return {}
        effective = Path(cwd).resolve()
        if not self.in_scope(effective) or self.nested_project(effective):
            return {}
        if event in ("SessionStart", "UserPromptSubmit"):
            self.ensure_started()
        root = str(self.root)
        session_id = "codex:" + payload["session_id"]
        with self.session(session_id, root) as state:
            if event == "SessionEnd":
                state.clear()
                return {}
            if event == "Interrupt":
                state["interrupted_turn"] = payload.get("turn_id")
                return {}
            if event == "SessionStart":
                if payload.get("source") not in ("compact", "resume"):
                    state.clear()
                    state["root"] = root
                text = self.session_context(root, state, budget=3300)
                return self.context(event, text) if text else {}
            if event == "UserPromptSubmit":
                state.pop("interrupted_turn", None)
                recalled = self.prompt_context(root, state, payload.get("prompt", ""), budget=1800)
                if not recalled:
                    return {}
                return self.context(event, recalled["additional_context"])
            if event == "PostToolUse":
                self.observe(payload, state, effective)
            elif event == "Stop":
                self.refresh_stop_task(root, state)
                return self.stop(payload, state)
        return {}

    def nested_project(self, cwd):
        if cwd == self.root:
            return False
        current = cwd
        boundary = repository_root(cwd) or self.root
        for parent in (current, *current.parents):
            manifest = parent / ".codex" / "memweave.json"
            if manifest.is_file():
                try:
                    installation = json.loads(manifest.read_text(encoding="utf-8"))
                    configured = installation.get("root")
                    if not isinstance(configured, str):
                        return True
                    configured_root = Path(configured).expanduser().resolve()
                    if configured_root != self.root and not (
                        repository_root(configured_root) == configured_root
                        and same_repository(configured_root, self.root)
                    ):
                        return True
                except (OSError, ValueError, TypeError, RecursionError):
                    return True
            if parent == boundary:
                break
        return False

    def in_scope(self, path):
        path = Path(path).expanduser().resolve()
        return path.is_relative_to(self.root) or same_repository(self.root, path)

    def source_scope(self, cwd):
        cwd = Path(cwd).expanduser().resolve()
        root = repository_root(cwd)
        return root if root is not None and same_repository(root, self.root) else cwd

    def own_path(self, value, cwd):
        if not isinstance(value, str) or not value.strip():
            return False
        path = Path(value)
        path = (path if path.is_absolute() else cwd / path).resolve()
        scope = self.source_scope(cwd)
        if not path.is_relative_to(scope) or path.is_relative_to(self.diagnostics.home):
            return False
        relative = path.relative_to(scope).as_posix()
        return not relative.startswith((".codex/", ".agents/skills/memweave-continuity/"))

    def observe(self, payload, state, cwd):
        name = payload.get("tool_name", "")
        parameters = payload.get("tool_input")
        response = payload.get("tool_response")
        if not isinstance(parameters, dict) or response is None:
            return
        prefix = "mcp__memweave__"
        if isinstance(name, str) and name.startswith(prefix):
            self.observe_tool({
                "mcp_server_name": "memweave", "tool_name": name[len(prefix):],
                "tool_input": json.dumps(parameters), "result_json": json.dumps(response),
            }, state)
            if state.get("task_id"):
                state.pop("unbound_work", None)
            return
        actual_work = False
        if name in ("apply_patch", "Edit", "Write"):
            if isinstance(response, dict) and (response.get("success") is False or response.get("isError")):
                return
            command = parameters.get("command", "")
            paths = re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
                               command, re.MULTILINE) if isinstance(command, str) else []
            paths += [parameters.get("file_path", parameters.get("path"))]
            actual_work = any(self.own_path(path, cwd) for path in paths)
        elif name in ("Bash", "exec_command", "functions.exec_command"):
            workdir = parameters.get("workdir", parameters.get("cwd"))
            if workdir is not None:
                if not isinstance(workdir, str):
                    return
                effective = Path(workdir)
                effective = (effective if effective.is_absolute() else cwd / effective).resolve()
                if not self.in_scope(effective):
                    return
            command = parameters.get("command", parameters.get("cmd", ""))
            actual_work = self.validation_command(command)
        if actual_work:
            state["needs_checkpoint" if state.get("task_id") else "unbound_work"] = True

    def validation_command(self, command):
        if not isinstance(command, str):
            return False
        try:
            tokens = shlex.split(command, posix=False)
        except ValueError:
            return False
        if tokens[:1] == ["&"]:
            tokens = tokens[1:]
        if not tokens:
            return False
        executable = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1]
        executable = re.sub(r"\.(exe|cmd|bat)$", "", executable.lower())
        arguments = tokens[1:]
        if executable == "pytest":
            return True
        if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?|py", executable):
            while arguments and arguments[0] in ("-X", "-I", "-B", "-u", "-E", "-s", "-S", "-O", "-OO"):
                arguments = arguments[2:] if arguments[0] == "-X" else arguments[1:]
            return len(arguments) >= 2 and arguments[0] == "-m" and arguments[1] in ("pytest", "unittest")
        if executable in ("npm", "pnpm"):
            arguments = arguments[1:] if arguments[:1] == ["run"] else arguments
            return arguments[:1] == ["test"]
        if executable in ("cargo", "go", "mvn", "gradle", "gradlew"):
            return arguments[:1] == ["test"]
        return executable == "ruff" and arguments[:1] == ["check"]

    def stop(self, payload, state):
        turn = payload.get("turn_id")
        if (not isinstance(turn, str) or not turn or payload.get("stop_hook_active") is not False
                or state.get("interrupted_turn") == turn or state.get("prompted_generation") == turn):
            return {}
        if state.get("task_id"):
            try:
                followup = self.followup(
                    {"generation_id": turn, "loop_count": 0, "status": "completed"}, state
                )
            except DiagnosticError:
                return {}
            if followup:
                return {"decision": "block", "reason": followup["followup_message"]}
        elif state.get("unbound_work"):
            state["prompted_generation"] = turn
            return {"decision": "block", "reason": (
                "MemWeave 观察到本轮项目改动或验证，但尚未关联任务。"
                "不要先用 shell 重读 Skill；调用 memweave_workspace_prepare，按当前真实目标显式准备或创建任务，"
                "再调用 memweave_task_checkpoint 保存真实发现、失败、验证结果与下一步。"
                "不要猜测旧任务，不要将未通过验证记为通过，不保存凭据或完整对话。"
                "只完成这一次记忆维护后结束，不扩展业务工作。"
            )}
        return {}
