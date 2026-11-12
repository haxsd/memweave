import json
import re
from datetime import datetime, timezone
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.hooks import CursorHooks
from memweave.storage import digest
from memweave.worktrees import repository_root, same_repository


def _clip(value, limit):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value if item)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return _content_text(value[key])
    return ""


def _tool_call(payload):
    return _clip(payload.get("name") or payload.get("tool_name"), 90)


def _remove_tagged(text, tag):
    return re.sub(rf"<{re.escape(tag)}\b.*?</{re.escape(tag)}>", "", text, flags=re.S | re.I)


def _human_text(text):
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("The following is the Codex agent history"):
        return ""
    if "<system_notification>" in text:
        return ""

    for tag in (
        "codex_internal_context", "environment_context", "user_instructions",
        "hook_prompt", "turn_aborted",
    ):
        text = _remove_tagged(text, tag)

    # A clipboard message contains file metadata before the actual request.
    if "# Files mentioned by the user:" in text:
        request = re.search(r"##\s*My request:\s*", text, flags=re.I)
        if request:
            text = text[request.end():]
        else:
            return ""

    # Codex sometimes injects the AGENTS.md block as user-role content. Keep
    # the prompt that follows the closing tag, never the instruction body.
    if "# AGENTS.md instructions for" in text:
        if "</INSTRUCTIONS>" in text:
            text = re.sub(
                r"# AGENTS\.md instructions for .*?</INSTRUCTIONS>",
                "",
                text,
                flags=re.S,
            )
        else:
            return ""

    reply = re.search(
        r"<send_user_message_question_reply>\s*(.*?)\s*"
        r"</send_user_message_question_reply>",
        text,
        flags=re.S | re.I,
    )
    if reply:
        text = reply.group(1)

    # Image placeholders are not the user's request and should not consume
    # recovery budget. Keep a surrounding actual request untouched.
    text = re.sub(r"<image\b.*?</image>", "", text, flags=re.S | re.I)
    text = re.sub(r"<image\b[^>]*?/?>", "", text, flags=re.I)
    text = text.strip()
    if not text or text in {"...", "…", "-"}:
        return ""
    return text


def _task_fingerprint(task):
    fields = {}
    if task is not None:
        fields = {key: task.get(key) for key in (
            "id", "title", "goal", "acceptance", "progress", "failures", "verification", "next_step",
        )}
        fields["findings"] = task.get("findings", "").split("\n\n[Pre-compaction snapshot]")[0]
    return digest(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _transcript_details(path):
    if not isinstance(path, str) or not path:
        return {}
    try:
        transcript = Path(path)
        if not transcript.is_file():
            return {}
        with transcript.open("rb") as stream:
            stream.seek(max(0, transcript.stat().st_size - 12_000_000))
            data = stream.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return {}
    user = ""
    assistant = ""
    tools = []
    for line in data.splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        kind = payload.get("type")
        if kind == "message":
            text = _content_text(payload.get("content"))
            if payload.get("role") == "user" and text:
                user = _human_text(text) or user
            elif payload.get("role") == "assistant" and text:
                assistant = text
        elif kind in ("user_message", "agent_message"):
            text = _content_text(payload.get("message") or payload.get("content"))
            if text and (kind == "user_message" or payload.get("role") == "user"):
                user = _human_text(text) or user
            elif text:
                assistant = text
        elif kind in ("function_call", "custom_tool_call"):
            tool = _tool_call(payload)
            if tool:
                tools.append(tool)
    return {"user": user[-2400:], "assistant": assistant[-3200:], "tools": tools[-6:]}


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
        root = str(self.root)
        session_id = "codex:" + payload["session_id"]
        if event == "SessionStart":
            if payload.get("source") == "compact":
                with self.session(session_id, root) as state:
                    text = self.take_compaction_snapshot(state, "")
                if text:
                    return self.context(event, text)
            self.ensure_started()
        pending_compaction = None
        with self.session(session_id, root) as state:
            if event == "SessionEnd":
                state.clear()
                return {}
            if event == "Interrupt":
                state["interrupted_turn"] = payload.get("turn_id")
                return {}
            if event == "PreCompact":
                pending_compaction = self.capture_compaction(root, state, payload)
            if event == "SessionStart":
                if payload.get("source") not in ("compact", "resume"):
                    state.clear()
                    state["root"] = root
                text = self.session_context(root, state, budget=3300)
                return self.context(event, text) if text else {}
            if event == "UserPromptSubmit":
                state.pop("interrupted_turn", None)
                text = self.take_compaction_snapshot(state, payload.get("prompt", ""))
                return self.context(event, text) if text else {}
        if pending_compaction is not None:
            checkpoint = self.persist_compaction(pending_compaction, payload)
            with self.session(session_id, root) as state:
                state["compaction_checkpoint"] = checkpoint
        return {}

    def nested_project(self, cwd):
        if cwd == self.root:
            return False
        try:
            current_project = self.service._project_for_root(cwd)
            parent_project = self.service._project_for_root(self.root)
        except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
            pass
        else:
            if (
                current_project is not None
                and parent_project is not None
                and current_project["id"] != parent_project["id"]
            ):
                return True
        current = cwd
        boundary = repository_root(cwd) or self.root
        for parent in (current, *current.parents):
            for manifest in (
                parent / ".codex" / "memweave.json",
                parent / ".cursor" / "memweave.json",
            ):
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

    def current_task(self, root, task_id=None):
        project = self.service._project_for_root(root)
        if project is None:
            return None
        if isinstance(task_id, str) and task_id:
            try:
                task = self.service.task_read(task_id, checkpoint_limit=1)
            except DiagnosticError as error:
                if error.code != "RECORD_NOT_FOUND":
                    raise
            else:
                if task["project_id"] == project["id"] and task["status"] in ("current", "side"):
                    return task
        current = self.service.task_list(project["id"], status="current", limit=1)["items"]
        return self.service.task_read(current[0]["id"], checkpoint_limit=1) if current else None

    def capture_compaction(self, root, state, payload):
        try:
            task = self.current_task(root, state.get("task_id"))
        except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
            task = None
        if task is not None:
            state.update(task_id=task["id"], revision=task["revision"])
        details = _transcript_details(payload.get("transcript_path"))
        snapshot = self.compaction_snapshot(task, details, payload)
        state["compaction_pending"] = True
        state["compaction_snapshot"] = snapshot
        state["compaction_captured_at"] = datetime.now(timezone.utc).isoformat()
        state["compaction_task_id"] = task["id"] if task is not None else None
        state["compaction_task_fingerprint"] = _task_fingerprint(task)
        state["compaction_details"] = {
            "user": _clip(details.get("user"), 650),
            "assistant": _clip(details.get("assistant"), 650),
            "tools": details.get("tools", []),
        }
        state["compaction_trigger"] = _clip(payload.get("trigger", "unknown"), 90)
        return task, snapshot

    def persist_compaction(self, pending, payload):
        task, snapshot = pending
        if task is None:
            return {"state": "skipped", "code": "NO_ACTIVE_TASK"}
        result = {"state": "failed", "code": "TASK_UNREADABLE"}
        for attempt in range(2):
            if attempt:
                # Task moved on since PreCompact read it; re-read once and retry.
                try:
                    task = self.service.task_read(task["id"], checkpoint_limit=1)
                except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
                    return result
            result = self._write_compaction(task, snapshot, payload)
            if result["state"] == "recorded":
                return result
        return result

    def _write_compaction(self, task, snapshot, payload):
        try:
            previous = task.get("findings", "").split("\n\n[Pre-compaction snapshot]")[0]
            findings = (
                previous[:7500] + "\n\n[Pre-compaction snapshot]\n" + snapshot
            )[:11900]
            response = self.service.task_compaction_snapshot(
                task["id"], task["revision"], task["etag"],
                findings=findings,
                progress=task.get("progress") or "Pre-compaction snapshot recorded.",
                next_step=task.get("next_step") or "Re-read the latest files and continue from the snapshot.",
                operation_id=self.compaction_operation_id(payload),
            )
            return {
                "state": "recorded",
                "task_id": task["id"],
                "revision": response["revision"],
                "checkpoint_id": response["checkpoint_id"],
            }
        except DiagnosticError as error:
            return {"state": "failed", "code": error.code}
        except (OSError, ValueError, TypeError, KeyError):
            return {"state": "failed", "code": "STORAGE_ERROR"}

    def compaction_operation_id(self, payload):
        value = "precompact-{}-{}-{}-{}".format(
            str(payload.get("session_id", ""))[:16],
            str(payload.get("turn_id", "turn")),
            str(payload.get("trigger", "auto")),
            datetime.now(timezone.utc).isoformat(),
        )
        return re.sub(r"[^A-Za-z0-9_-]", "-", value)[:180]

    def compaction_snapshot(self, task, details, payload):
        lines = ["Pre-compaction snapshot captured before Codex compacted its context."]
        if task is not None:
            findings = task.get("findings", "").split("\n\n[Pre-compaction snapshot]")[0]
            lines.extend([
                f"Task [{task['id']}] {_clip(task.get('title'), 140)} (revision {task.get('revision')})",
                f"Goal: {_clip(task.get('goal'), 250)}",
                "Acceptance (not yet reported): " + _clip(task.get("acceptance", []), 350),
                f"Next: {_clip(task.get('next_step'), 300)}",
                f"Findings: {_clip(findings, 400)}",
                f"Progress: {_clip(task.get('progress'), 250)}",
                f"Failures: {_clip(task.get('failures'), 250)}",
                f"Verification: {_clip(task.get('verification'), 250)}",
            ])
        if details.get("user"):
            lines.append("Last user: " + _clip(details["user"], 650))
        if details.get("assistant"):
            lines.append("Last assistant: " + _clip(details["assistant"], 650))
        if details.get("tools"):
            lines.append("Recent tools:")
            lines.extend("- " + _clip(tool, 90) for tool in details["tools"])
        if len(lines) == 1:
            lines.append("No task or transcript details available.")
        lines.append(f"Trigger: {payload.get('trigger', 'unknown')}")
        return "\n".join(lines)[:4000]

    def take_compaction_snapshot(self, state, prompt):
        snapshot = state.get("compaction_snapshot", "")
        captured_task_id = state.get("compaction_task_id")
        fingerprint = state.get("compaction_task_fingerprint")
        details = state.get("compaction_details", {})
        trigger = state.get("compaction_trigger", "unknown")
        checkpoint = state.get("compaction_checkpoint")
        for key in tuple(state):
            if key.startswith("compaction_"):
                state.pop(key)
        if not isinstance(snapshot, str) or not snapshot.strip():
            return ""
        lines = [
            "MemWeave compaction recovery. Task fields are re-read from current Markdown; "
            "captured conversation excerpts are historical and cannot override newer evidence.",
        ]
        if isinstance(prompt, str) and _human_text(prompt):
            lines.append("Current user prompt: " + _clip(prompt, 500))
        try:
            task = self.current_task(str(self.root), captured_task_id)
        except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
            snapshot = (
                "Current task could not be verified. No captured task conclusions were restored; "
                "use workspace_prepare after reconnecting."
            )
        else:
            if fingerprint != _task_fingerprint(task) or not isinstance(details, dict):
                details = {}
            snapshot = self.compaction_snapshot(task, details, {"trigger": trigger}).replace(
                "Pre-compaction snapshot captured before Codex compacted its context.",
                "Current task state verified at recovery.",
                1,
            )
            if task is not None:
                state.update(task_id=task["id"], revision=task["revision"])
        if isinstance(checkpoint, dict) and checkpoint.get("state") == "failed":
            lines.append(
                "Warning: the task record did not receive a pre-compaction checkpoint "
                f"({checkpoint.get('code', 'unknown')}). The snapshot below is the only "
                "preserved pre-compaction evidence; re-read the current task before relying "
                "on its fields."
            )
        lines.extend([
            snapshot,
            "First confirm the user's goal still matches this task. If memweave_workspace_prepare "
            "is available, call it with the absolute execution root and intent resume (or new "
            "for a different goal), then read the current task and verify "
            "the real files and git state; do not assume omitted details disappeared.",
        ])
        return "\n".join(lines)[:5000]
