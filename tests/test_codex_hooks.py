import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memweave.services import Service


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "home"
        self.service = Service(self.home)
        self.service.initialize()
        self.project = self.call("project_register", root=str(self.root), name="test", operation_id="project")

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def hook(self, event, **parameters):
        payload = dict(session_id="codex-session", cwd=str(self.root), hook_event_name=event, turn_id="turn-1")
        payload.update(parameters)
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", "hook", "--client", "codex", "--event", event,
             "--root", str(self.root), "--home", str(self.home)],
            input=json.dumps(payload), capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, cwd=self.temporary.name, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "", completed.stderr)
        return json.loads(completed.stdout)

    def task(self):
        return self.call("task_open", project_id=self.project["id"], title="fix", goal="verify change",
                         acceptance=["regression passes"], operation_id="task")

    def observe(self, name, parameters, response):
        return self.hook("PostToolUse", tool_name="mcp__memweave__memweave_" + name,
                         tool_input=parameters, tool_response={"structuredContent": {"status": "ok", "data": response}})

    def test_start_context_uses_codex_protocol_without_auto_resuming_old_task(self):
        self.call("memory_write", project_id=self.project["id"], title="policy", body="PROJECT_POLICY_VISIBLE",
                  sources=[{"type": "user", "reference": "explicit"}], operation_id="knowledge")
        self.task()
        response = self.hook("SessionStart", source="startup")
        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("PROJECT_POLICY_VISIBLE", output["additionalContext"])
        self.assertNotIn("verify change", output["additionalContext"])
        self.assertIn(".agents/skills/memweave-continuity", output["additionalContext"])

    def test_real_work_without_task_prompts_capture_once_without_fabricating_records(self):
        self.hook("PostToolUse", tool_name="apply_patch",
                  tool_input={"command": "*** Begin Patch\n*** Add File: main.py\n+pass\n*** End Patch"},
                  tool_response={"success": True})
        response = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("memweave_workspace_prepare", response["reason"])
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.assertEqual(self.hook("Stop", stop_hook_active=True, turn_id="followup"), {})
        self.assertEqual(self.call("task_list", project_id=self.project["id"])["items"], [])

    def test_task_checkpoint_and_consolidation_are_requested_from_actual_state(self):
        task = self.task()
        self.observe("task_open", {"project_id": self.project["id"]}, task)
        response = self.hook("Stop", stop_hook_active=False)
        self.assertIn("memweave_task_checkpoint", response.get("reason", ""))
        saved = self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                          findings="located cause", progress="not tested", next_step="run regression", operation_id="cp")
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        self.assertEqual(self.hook("Stop", stop_hook_active=False, turn_id="turn-2"), {})
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "python -m unittest"},
                  tool_response={"exit_code": 1, "stdout": "PRIVATE_OUTPUT"})
        self.assertIn("memweave_task_checkpoint", self.hook(
            "Stop", stop_hook_active=False, turn_id="turn-2"
        ).get("reason", ""))

    def test_resume_after_compaction_uses_explicitly_bound_task_and_preserves_privacy(self):
        task = self.task()
        self.observe("task_open", {}, task)
        saved = self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                          findings="cause", progress="unverified", next_step="NEXT_ACTION_PRESERVED", operation_id="cp")
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        compact = self.hook("SessionStart", source="compact", transcript_path="PRIVATE_TRANSCRIPT")
        self.assertIn("NEXT_ACTION_PRESERVED", compact["hookSpecificOutput"]["additionalContext"])
        with self.service.vault.diagnostics._connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("PRIVATE_TRANSCRIPT", dump)

    def test_interrupt_foreign_edits_and_failed_mcp_calls_do_not_restart_or_bind(self):
        self.hook("PostToolUse", tool_name="apply_patch",
                  tool_input={"command": "*** Begin Patch\n*** Add File: ../foreign.py\n+pass\n*** End Patch"},
                  tool_response={"success": True})
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "pytest"},
                  tool_response={"exit_code": 0})
        self.hook("Interrupt")
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})

    def test_completed_task_prompts_review_then_stops_after_real_review(self):
        task = self.task()
        self.observe("task_open", {}, task)
        closed = self.call(
            "task_close", task_id=task["id"], expected_revision=task["revision"], expected_etag=task["etag"],
            outcome="completed", summary="fixture completed", acceptance_results={"regression passes": "passed fixture"},
            operation_id="close",
        )
        self.observe("task_close", {"task_id": task["id"]}, closed)
        self.assertIn("memweave_memory_review", self.hook("Stop", stop_hook_active=False)["reason"])
        review = self.call("memory_review", task_id=task["id"])
        self.call(
            "memory_review_complete", task_id=task["id"], expected_revision=review["task"]["revision"],
            expected_etag=review["task"]["etag"], memory_ids=[], summary="No reusable knowledge.", operation_id="review",
        )
        self.assertEqual(self.hook("Stop", stop_hook_active=False, turn_id="turn-2"), {})

    def test_failed_mcp_foreign_task_and_replayed_checkpoint_cannot_hide_new_work(self):
        task = self.task()
        self.hook("PostToolUse", tool_name="mcp__memweave__memweave_task_open", tool_input={},
                  tool_response={"isError": True, "structuredContent": {"status": "ok", "data": task}})
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.observe("task_open", {}, task)
        saved = self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="cause", progress="unverified", next_step="verify", operation_id="cp",
        )
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "pytest"},
                  tool_response={"exit_code": 1})
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        self.assertIn("memweave_task_checkpoint", self.hook("Stop", stop_hook_active=False)["reason"])

    def test_session_end_clears_task_binding_and_never_persists_output(self):
        task = self.task()
        self.observe("task_open", {}, task)
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "pytest PRIVATE_COMMAND"},
                  tool_response={"exit_code": 1, "stdout": "PRIVATE_OUTPUT"})
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        with self.service.vault.diagnostics._connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("PRIVATE_COMMAND", dump)
        self.assertNotIn("PRIVATE_OUTPUT", dump)

    def test_read_only_mentions_and_foreign_validation_do_not_request_task_writes(self):
        for command in ("rg pytest README.md", "echo pytest", "python -c \"print('pytest')\""):
            self.hook("PostToolUse", tool_name="Bash", tool_input={"command": command},
                      tool_response={"exit_code": 0})
            self.assertEqual(self.hook("Stop", stop_hook_active=False), {}, command)
        foreign = Path(self.temporary.name) / "foreign"
        foreign.mkdir()
        self.hook("PostToolUse", tool_name="Bash",
                  tool_input={"command": "pytest", "workdir": str(foreign)}, tool_response={"exit_code": 1})
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
