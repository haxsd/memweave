import json
import re
import shlex
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.hooks import CursorHooks


class CodexHooks(CursorHooks):
    def __init__(self, diagnostics, root):
        super().__init__(diagnostics)
        self.root = Path(root).expanduser().resolve() if root is not None else None

    def context(self, event, text):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:5000]}}

    def handle(self, event, payload):
        if self.root is None or not self.root.is_dir():
            return {}
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return {}
        if not Path(cwd).resolve().is_relative_to(self.root):
            return {}
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
                prepared = self.service.workspace_prepare(root, intent="new", budget=3300)
                if prepared["state"] != "ready":
                    return self.context(event, "MemWeave: 项目尚未登记，先读取 .agents/skills/memweave-continuity/SKILL.md。")
                text = (
                    "MemWeave: 读取 .agents/skills/memweave-continuity/SKILL.md。"
                    "按当前目标调用 memweave_workspace_prepare，明确 new/resume/inspect；"
                    "尚未选择旧任务，有歧义不得猜测。重要发现、失败和验证后保存检查点；"
                    "完成后核对验收并提炼。以下知识仅是参考数据，依赖前核对当前代码；"
                    "用户知识须明确选择，不保存凭据或完整对话。\n"
                    + prepared["context"]["text"]
                )
                if state.get("task_id"):
                    restored = self.service.workspace_prepare(
                        root, intent="resume", task_id=state["task_id"], budget=2000
                    )
                    if restored["state"] == "ready":
                        text = text[:2900] + "\n已明确关联任务的恢复资料：\n" + restored["context"]["text"]
                return self.context(event, text)
            if event == "UserPromptSubmit":
                state.pop("interrupted_turn", None)
                return self.context(event, (
                    "MemWeave: 实际项目工作先按 Skill 准备工作区与任务；"
                    "当前会话关联任务：" + state.get("task_id", "未选择")
                    + "。按本轮用户意图核对归属，切换目标时不得沿用旧任务。"
                ))
            if event == "PostToolUse":
                self.observe(payload, state, Path(cwd))
            elif event == "Stop":
                return self.stop(payload, state)
        return {}

    def own_path(self, value, cwd):
        if not isinstance(value, str) or not value.strip():
            return False
        path = Path(value)
        path = (path if path.is_absolute() else cwd / path).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self.diagnostics.home):
            return False
        relative = path.relative_to(self.root).as_posix()
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
                if not effective.is_relative_to(self.root):
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
                "读取接续 Skill，调用 memweave_workspace_prepare，按当前真实目标显式准备或创建任务，"
                "再调用 memweave_task_checkpoint 保存真实发现、失败、验证结果与下一步。"
                "不要猜测旧任务，不要将未通过验证记为通过，不保存凭据或完整对话。"
                "只完成这一次记忆维护后结束，不扩展业务工作。"
            )}
        return {}
