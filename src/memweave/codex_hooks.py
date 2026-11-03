import json
import re
from datetime import datetime, timezone
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.hooks import CursorHooks
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
                user = text
            elif payload.get("role") == "assistant" and text:
                assistant = text
        elif kind in ("user_message", "agent_message"):
            text = _content_text(payload.get("message") or payload.get("content"))
            if text and (kind == "user_message" or payload.get("role") == "user"):
                user = text
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
            self.persist_compaction(pending_compaction, payload)
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

    def capture_compaction(self, root, state, payload):
        task = None
        task_id = state.get("task_id")
        if isinstance(task_id, str) and task_id:
            try:
                task = self.service.task_read(task_id, checkpoint_limit=1)
            except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
                task = None
        if task is None or task["status"] != "current":
            task = None
            try:
                project = self.service._project_for_root(root)
                if project is not None:
                    current = self.service.task_list(project["id"], status="current", limit=1)["items"]
                    if current:
                        task = self.service.task_read(current[0]["id"], checkpoint_limit=1)
            except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
                pass
        if task is not None:
            state.update(task_id=task["id"], revision=task["revision"])
        details = _transcript_details(payload.get("transcript_path"))
        snapshot = self.compaction_snapshot(task, details, payload)
        state["compaction_pending"] = True
        state["compaction_snapshot"] = snapshot
        state["compaction_captured_at"] = datetime.now(timezone.utc).isoformat()
        return task, snapshot

    def persist_compaction(self, pending, payload):
        task, snapshot = pending
        if task is None:
            return
        try:
            previous = task.get("findings", "").split("\n\n[Pre-compaction snapshot]")[0]
            findings = (
                previous[:7500] + "\n\n[Pre-compaction snapshot]\n" + snapshot
            )[:11900]
            self.service.task_checkpoint(
                task["id"], task["revision"], task["etag"],
                findings=findings,
                progress=task.get("progress") or "Pre-compaction snapshot recorded.",
                failures=task.get("failures", ""),
                verification=task.get("verification", ""),
                next_step=task.get("next_step") or "Re-read the latest files and continue from the snapshot.",
                operation_id=self.compaction_operation_id(payload),
            )
        except (DiagnosticError, OSError, ValueError, TypeError, KeyError):
            pass

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
        snapshot = state.pop("compaction_snapshot", "")
        state.pop("compaction_pending", None)
        state.pop("compaction_captured_at", None)
        if not isinstance(snapshot, str) or not snapshot.strip():
            return ""
        lines = [
            "MemWeave compaction recovery. The previous Codex context was compacted; "
            "this is a bounded snapshot captured before compaction, not current truth.",
        ]
        if prompt:
            lines.append("Current user prompt: " + _clip(prompt, 500))
        lines.extend([
            snapshot,
            "First confirm the user's goal still matches this task. If memweave_workspace_prepare "
            "is available, call it with the absolute execution root and intent resume (or new "
            "for a different goal), then read the current task and verify "
            "the real files and git state; do not assume omitted details disappeared.",
        ])
        return "\n".join(lines)[:5000]
