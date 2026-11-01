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

    def test_start_context_uses_codex_protocol_and_auto_resumes_unique_task(self):
        self.call("memory_write", project_id=self.project["id"], title="policy",
                  body="PROJECT_POLICY_VISIBLE verify change regression",
                  sources=[{"type": "user", "reference": "explicit"}], operation_id="knowledge")
        self.task()
        response = self.hook("SessionStart", source="startup")
        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("PROJECT_POLICY_VISIBLE", output["additionalContext"])
        self.assertIn("verify change", output["additionalContext"])
        self.assertIn("唯一 current 任务", output["additionalContext"])
        self.assertIn("memweave-continuity", output["additionalContext"])

    def test_new_session_restores_current_checkpoint_and_never_resumes_queued_task(self):
        task = self.task()
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="root cause located",
            progress="regression still failing", next_step="NEXT_RESUME_ACTION",
            operation_id="resume-checkpoint",
        )
        restored = self.hook("SessionStart", source="startup", session_id="new-session")
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn(task["id"], text)
        self.assertIn("NEXT_RESUME_ACTION", text)
        second = self.call(
            "task_open", project_id=self.project["id"], title="second task", goal="SECOND_TASK_GOAL",
            acceptance=["second acceptance"], operation_id="second-task",
        )
        self.assertEqual(second["status"], "queued")
        resumed = self.hook("SessionStart", source="startup", session_id="third-session")
        context = resumed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("唯一 current 任务", context)
        self.assertIn(task["id"], context)
        self.assertIn("NEXT_RESUME_ACTION", context)
        self.assertNotIn(second["id"], context)

    def test_parent_project_hook_does_not_inject_into_nested_memweave_project(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".codex").mkdir()
        (nested / ".codex" / "memweave.json").write_text(
            json.dumps({"root": str(nested)}), encoding="utf-8"
        )
        self.task()
        response = self.hook("SessionStart", source="startup", cwd=str(nested))
        self.assertEqual(response, {})

    def test_user_prompt_recalls_by_user_wording_deduplicates_and_does_not_persist_prompt(self):
        self.call(
            "memory_write", project_id=self.project["id"], title="任务接续方法",
            body=(
                "切换工具前保存检查点，恢复时读取当前任务、最新进展和下一步。\n"
                "Retrieval cues: resume previous work in another client, where left off"
            ),
            sources=[{"type": "user", "reference": "task continuity"}],
            operation_id="prompt-recall",
        )
        prompt = "resume previous work in another client PRIVATE_PROMPT_TOKEN_9f3a"
        first = self.hook("UserPromptSubmit", prompt=prompt, turn_id="turn-2")
        text = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("任务接续方法", text)
        self.assertIn("切换工具前保存检查点", text)
        self.assertIn("可能不适用", text)
        self.assertEqual(self.hook("UserPromptSubmit", prompt=prompt, turn_id="turn-3"), {})
        with self.service.vault.diagnostics._connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("PRIVATE_PROMPT_TOKEN_9f3a", dump)

    def test_paraphrased_question_without_lexical_match_does_not_inject_recent_background(self):
        self.call(
            "memory_write", project_id=self.project["id"], title="任务接续方法",
            body="切换工具前保存检查点，恢复时读取当前任务和下一步。",
            sources=[{"type": "user", "reference": "explicit"}],
            operation_id="continuity",
        )
        self.call(
            "memory_write", project_id=self.project["id"], title="验收候选知识",
            body="候选知识经核对并明确启用后，才进入常规检索与上下文。",
            sources=[{"type": "host", "reference": "validation"}],
            operation_id="acceptance",
        )
        response = self.hook(
            "UserPromptSubmit", prompt="怎样在新窗口知道旧线程做到哪里", turn_id="turn-2"
        )
        text = response.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("任务接续方法", text)
        self.assertNotIn("recent background", text)

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
        self.service.start()
        self.addCleanup(self.service.close)
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
        self.service.start()
        self.addCleanup(self.service.close)
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
