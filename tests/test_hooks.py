import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from memweave.services import Service


class HookWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "项目"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "memory home"
        self.service = Service(self.home)
        self.service.initialize()
        self.project = self.call("project_register", root=str(self.root), name="项目", operation_id="project")

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def hook(self, event, **parameters):
        payload = {
            "conversation_id": "conversation-1", "session_id": "conversation-1",
            "generation_id": "generation-1", "workspace_roots": [str(self.root)],
            "hook_event_name": event, **parameters,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        completed = subprocess.run(
            [sys.executable, "-m", "memweave", "hook", "--client", "cursor",
             "--event", event, "--home", str(self.home)],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, cwd=self.temporary.name, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "", completed.stderr)
        return json.loads(completed.stdout)

    def task(self, operation_id="task"):
        return self.call("task_open", project_id=self.project["id"], title="实现修复",
                         goal="OLD_TASK_GOAL_MUST_NOT_LEAK", acceptance=["通过回归"],
                         operation_id=operation_id)

    def observe(self, action, parameters, response, **event_parameters):
        payload = dict(mcp_server_name="memweave", tool_name="memweave_" + action,
                       tool_input=json.dumps(parameters),
                       result_json=json.dumps({"status": "ok", "data": response}))
        payload.update(event_parameters)
        return self.hook("afterMCPExecution", **payload)

    def checkpoint(self, task, operation_id="checkpoint"):
        return self.call("task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
                         expected_etag=task["etag"], findings="定位了真实原因", progress="回归已通过",
                         next_step="检查调用方", operation_id=operation_id)

    def test_start_auto_resumes_unique_task_and_withholds_user_scope(self):
        self.call("memory_write", title="项目约束",
                  body="PROJECT_RULE_VISIBLE 实现修复 OLD_TASK_GOAL_MUST_NOT_LEAK",
                  project_id=self.project["id"],
                  sources=[{"type": "user", "reference": "明确约束"}], operation_id="memory")
        self.call("memory_write", title="用户偏好", body="PRIVATE_USER_PREFERENCE", scope="user",
                  sources=[{"type": "user", "reference": "明确偏好"}], operation_id="user")
        self.task()
        response = self.hook("sessionStart")
        context = response.get("additional_context", "")
        self.assertIn("PROJECT_RULE_VISIBLE", context)
        self.assertIn("memweave_workspace_prepare", context)
        self.assertIn("不要用 shell 重读 Skill", context)
        self.assertNotIn("读取 memweave-continuity Skill", context)
        self.assertIn("OLD_TASK_GOAL_MUST_NOT_LEAK", context)
        self.assertIn("唯一 current 任务", context)
        self.assertIn("MemWeave · 任务", context)
        self.assertNotIn("PRIVATE_USER_PREFERENCE", context)
        self.assertLessEqual(len(context), 6000)
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})
        self.assertEqual(self.call("task_list", project_id=self.project["id"])["items"][0]["revision"], 1)

    def test_start_without_a_task_still_returns_project_context_and_status_line(self):
        response = self.hook("sessionStart", generation_id="")
        context = response.get("additional_context", "")
        self.assertIn("MEMWEAVE CONTEXT", context)
        self.assertIn("MemWeave · 尚未建立任务", context)

    def test_after_mcp_fallback_restores_context_once_when_session_start_is_missing(self):
        self.service.start()
        self.addCleanup(self.service.close)
        task = self.task()
        prepared = self.call(
            "workspace_prepare", root=str(self.root), intent="resume", task_id=task["id"]
        )
        response = self.observe(
            "workspace_prepare",
            {"root": str(self.root), "intent": "resume", "task_id": task["id"]},
            prepared,
        )
        context = response.get("additional_context", "")
        self.assertIn("Cursor 启动上下文未确认送达时的一次性恢复上下文", context)
        self.assertIn(task["id"], context)
        self.assertIn("MemWeave · 任务", context)
        self.assertEqual(
            self.observe(
                "workspace_prepare",
                {"root": str(self.root), "intent": "resume", "task_id": task["id"]},
                prepared,
            ),
            {},
        )

    def test_emitted_startup_context_is_not_repeated_after_status_and_inspect(self):
        self.service.start()
        self.addCleanup(self.service.close)
        response = self.hook("sessionStart")
        self.assertIn("MEMWEAVE CONTEXT", response.get("additional_context", ""))
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            state = json.loads(
                connection.execute(
                    "SELECT state FROM hook_sessions WHERE session_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
            )
        self.assertIs(state["startup_context_emitted"], True)
        self.assertEqual(state["startup_context_source"], "sessionStart")
        self.assertNotIn("context_delivered", state)
        self.assertEqual(self.observe("system_status", {}, {"status": "ok"}), {})
        prepared = self.call(
            "workspace_prepare", root=str(self.root), intent="inspect"
        )
        self.assertEqual(
            self.observe(
                "workspace_prepare",
                {"root": str(self.root), "intent": "inspect"},
                prepared,
            ),
            {},
        )

    def test_legacy_delivery_flag_migrates_to_emitted_without_model_claim(self):
        response = self.hook("sessionStart")
        self.assertIn("MEMWEAVE CONTEXT", response.get("additional_context", ""))
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            state = json.loads(
                connection.execute(
                    "SELECT state FROM hook_sessions WHERE session_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
            )
            state.pop("startup_context_emitted", None)
            state.pop("startup_context_source", None)
            state["context_delivered"] = True
            state["context_source"] = "sessionStart"
            connection.execute(
                "UPDATE hook_sessions SET state = ? WHERE session_id = ?",
                (json.dumps(state, ensure_ascii=False), "conversation-1"),
            )
            connection.commit()
        self.assertEqual(self.observe("system_status", {}, {"status": "ok"}), {})
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            migrated = json.loads(
                connection.execute(
                    "SELECT state FROM hook_sessions WHERE session_id = ?",
                    ("conversation-1",),
                ).fetchone()[0]
            )
        self.assertIs(migrated["startup_context_emitted"], True)
        self.assertEqual(migrated["startup_context_source"], "sessionStart")
        self.assertNotIn("context_delivered", migrated)

    def test_unknown_or_ambiguous_workspace_never_loads_another_projects_context(self):
        unknown = Path(self.temporary.name) / "unknown"
        unknown.mkdir()
        for roots in ([], [str(unknown)], [str(self.root), str(unknown)], ["."], "invalid"):
            with self.subTest(roots=roots):
                response = self.hook("sessionStart", workspace_roots=roots)
                self.assertNotIn("OLD_TASK_GOAL_MUST_NOT_LEAK", str(response))
                self.assertEqual(response, {})
        self.assertEqual(len(self.call("project_list")["items"]), 1)

    def test_cursor_drive_prefixed_windows_root_is_normalized(self):
        if os.name != "nt":
            self.skipTest("Windows drive-prefix normalization is Windows-specific")
        response = self.hook(
            "sessionStart",
            workspace_roots=["/" + str(self.root).replace("\\", "/")],
            generation_id="",
        )
        self.assertIn("MEMWEAVE CONTEXT", response.get("additional_context", ""))

    def test_successful_open_gets_one_checkpoint_reminder_and_never_closes_task(self):
        task = self.task()
        self.observe("task_open", {"project_id": self.project["id"]}, task)
        response = self.hook("stop", status="completed", loop_count=0)
        self.assertIn(task["id"], response.get("followup_message", ""))
        self.assertIn("memweave_task_checkpoint", response["followup_message"])
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})
        self.assertEqual(self.hook("stop", status="completed", loop_count=1, generation_id="auto-loop"), {})
        current = self.call("task_read", task_id=task["id"])
        self.assertEqual(current["status"], "current")
        self.assertEqual(current["revision"], 1)

    def test_checkpoint_suppresses_reminder_until_another_edit(self):
        task = self.task()
        self.observe("task_open", {}, task)
        checkpoint = self.checkpoint(task)
        self.observe("task_checkpoint", {"task_id": task["id"]}, checkpoint)
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})
        self.hook("afterFileEdit", file_path=str(self.root / "main.py"), edits="PRIVATE_FILE_CONTENT")
        response = self.hook("stop", status="completed", loop_count=0)
        self.assertIn("memweave_task_checkpoint", response.get("followup_message", ""))
        self.assertNotIn("PRIVATE_FILE_CONTENT", str(response))

    def test_completed_task_gets_consolidation_reminder_only_until_reviewed(self):
        task = self.task()
        closed = self.call("task_close", task_id=task["id"], expected_revision=1,
                           expected_etag=task["etag"], outcome="completed", summary="已验证",
                           acceptance_results={"通过回归": "已运行并通过"}, operation_id="close")
        response = self.hook("stop", status="completed", loop_count=0)
        self.assertIn("memweave_memory_review", response.get("followup_message", ""))
        self.call("memory_review_complete", task_id=task["id"], expected_revision=closed["revision"],
                  expected_etag=closed["etag"], memory_ids=[], summary="没有额外可复用知识", operation_id="review")
        self.assertEqual(self.hook("stop", status="completed", loop_count=0, generation_id="next"), {})

    def test_aborted_error_and_other_conversations_do_not_trigger_followups(self):
        task = self.task()
        self.observe("task_open", {}, task)
        for overrides in (
            {"status": "aborted"}, {"status": "error"}, {"status": "unknown"},
            {"conversation_id": "another", "session_id": "another"},
            {"loop_count": 1}, {"loop_count": True}, {"loop_count": -1}, {"generation_id": None},
        ):
            with self.subTest(overrides=overrides):
                payload = dict(status="completed", loop_count=0)
                payload.update(overrides)
                self.assertEqual(self.hook("stop", **payload), {})

    def test_explicit_resume_binds_but_new_inspect_and_session_end_clear_binding(self):
        self.service.start()
        self.addCleanup(self.service.close)
        task = self.task()
        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume", task_id=task["id"])
        self.observe("workspace_prepare", {"root": str(self.root), "intent": "resume"}, prepared)
        self.assertIn(task["id"], self.hook("stop", status="completed", loop_count=0)["followup_message"])
        for intent in ("new", "inspect"):
            prepared = self.call("workspace_prepare", root=str(self.root), intent=intent)
            self.observe("workspace_prepare", {"root": str(self.root), "intent": intent}, prepared)
            self.assertEqual(self.hook("stop", status="completed", loop_count=0, generation_id=intent), {})
        self.observe("task_open", {}, task)
        self.hook("sessionEnd")
        self.assertEqual(self.hook("stop", status="completed", loop_count=0, generation_id="ended"), {})

    def test_failed_other_server_and_cross_project_tool_results_cannot_bind(self):
        task = self.task()
        other_root = Path(self.temporary.name) / "other"
        other_root.mkdir()
        other = self.call("project_register", root=str(other_root), name="other", operation_id="other")
        foreign = self.call("task_open", project_id=other["id"], title="别的项目", goal="不关联",
                            acceptance=["不关联"], operation_id="foreign")
        self.observe("task_open", {}, foreign)
        self.observe("task_open", {}, task, mcp_server_name="other-server")
        self.hook("afterMCPExecution", mcp_server_name="memweave", tool_name="memweave_task_open",
                  tool_input="{}", result_json=json.dumps({"status": "error", "data": task}))
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})

    def test_hook_never_reads_transcript_or_persists_tool_and_file_bodies(self):
        task = self.task()
        self.observe("task_open", {"secret": "PRIVATE_TOOL_PARAMETER"}, task)
        self.hook("afterFileEdit", edits="PRIVATE_EDIT_BODY", transcript_path="PRIVATE_TRANSCRIPT")
        self.hook("stop", status="completed", loop_count=0)
        with self.service.vault.diagnostics._connect() as connection:
            dump = "\n".join(connection.iterdump())
        for private in ("PRIVATE_TOOL_PARAMETER", "PRIVATE_EDIT_BODY", "PRIVATE_TRANSCRIPT"):
            self.assertNotIn(private, dump)

    def test_parallel_duplicate_stops_submit_only_one_followup(self):
        task = self.task()
        self.observe("task_open", {}, task)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda unused: self.hook("stop", status="completed", loop_count=0), range(2)
            ))
        self.assertEqual(sum("followup_message" in response for response in responses), 1)

    def test_queued_task_cannot_bind_and_session_end_cleans_only_its_session(self):
        first = self.task()
        second = self.task(operation_id="second")
        self.assertEqual(first["status"], "current")
        self.assertEqual(second["status"], "queued")
        self.observe("task_open", {}, first)
        self.observe("task_open", {}, second, conversation_id="second", session_id="second")
        self.hook("sessionEnd")
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})
        response = self.hook("stop", status="completed", loop_count=0,
                             conversation_id="second", session_id="second")
        self.assertEqual(response, {})

    def test_replayed_old_revision_does_not_dirty_a_saved_checkpoint(self):
        task = self.task()
        self.observe("task_open", {}, task)
        saved = self.checkpoint(task)
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        self.observe("task_open", {}, task)
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})

    def test_preparing_an_unknown_workspace_drops_the_previous_task_binding(self):
        task = self.task()
        self.observe("task_open", {}, task)
        unknown = Path(self.temporary.name) / "unknown"
        unknown.mkdir()
        prepared = self.call("workspace_prepare", root=str(unknown), intent="new")
        self.observe("workspace_prepare", {"root": str(unknown), "intent": "new"}, prepared)
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})

    def test_foreign_file_edits_and_cancelled_tasks_do_not_schedule_work(self):
        task = self.task()
        saved = self.checkpoint(task)
        self.observe("task_checkpoint", {"task_id": task["id"]}, saved)
        self.hook("afterFileEdit", file_path=str(Path(self.temporary.name) / "foreign.py"))
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})
        closed = self.call("task_close", task_id=task["id"], expected_revision=saved["revision"],
                           expected_etag=saved["etag"], outcome="cancelled", summary="用户取消",
                           acceptance_results={}, operation_id="cancel")
        self.observe("task_close", {"task_id": task["id"]}, closed)
        self.assertEqual(self.hook("stop", status="completed", loop_count=0), {})


if __name__ == "__main__":
    unittest.main()
import concurrent.futures
