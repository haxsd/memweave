import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memweave.codex_hooks import CodexHooks, _transcript_details
from memweave.diagnostics import DiagnosticError
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
        self.assertIn("不要用 shell 重读 Skill", output["additionalContext"])
        self.assertIn("用户纠正、取消旧决定或新证据推翻当前方案", output["additionalContext"])
        self.assertNotIn("读取 memweave-continuity Skill", output["additionalContext"])

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
        self.task()
        for adapter in (".codex", ".cursor"):
            with self.subTest(adapter=adapter):
                nested = self.root / ("nested-" + adapter.removeprefix("."))
                nested.mkdir()
                (nested / adapter).mkdir()
                (nested / adapter / "memweave.json").write_text(
                    json.dumps({"root": str(nested)}), encoding="utf-8"
                )
                response = self.hook("SessionStart", source="startup", cwd=str(nested))
                self.assertEqual(response, {})

    def test_user_prompt_without_compaction_snapshot_is_empty_and_does_not_persist_prompt(self):
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
        self.assertEqual(first, {})
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

    def test_resume_after_compaction_finds_current_task_and_preserves_privacy(self):
        task = self.task()
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                  findings="cause " * 600, progress="unverified " * 300,
                  failures="unresolved " * 300, next_step="NEXT_ACTION_PRESERVED", operation_id="cp")
        receipts = self.service.vault.diagnostics.status()["data"]["hook_receipt_count"]
        self.assertEqual(self.hook("UserPromptSubmit", prompt="ordinary prompt"), {})
        self.assertEqual(
            self.service.vault.diagnostics.status()["data"]["hook_receipt_count"],
            receipts,
        )
        transcript = Path(self.temporary.name) / "transcript.jsonl"
        transcript.write_text(
            '{"type":"response_item","payload":{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"PRECOMPACT_USER_DETAIL"}]}}\n'
            + json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "PRECOMPACT_ASSISTANT_DETAIL " + "context " * 300}],
            }}) + "\n"
            + '{"type":"response_item","payload":{"type":"function_call",'
            '"name":"exec_command","arguments":"{\\"cmd\\":\\"curl -H Bearer_SYNTHETIC_SECRET\\"}"}}\n',
            encoding="utf-8",
        )
        self.assertEqual(
            self.hook("PreCompact", trigger="auto", transcript_path=str(transcript)),
            {},
        )
        compact = self.hook("UserPromptSubmit", prompt="continue after compaction")
        text = compact["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NEXT_ACTION_PRESERVED", text)
        self.assertIn("PRECOMPACT_USER_DETAIL", text)
        self.assertIn("PRECOMPACT_ASSISTANT_DETAIL", text)
        self.assertLessEqual(len(text), 5000)
        self.assertIn("exec_command", text)
        self.assertNotIn("Bearer_SYNTHETIC_SECRET", text)
        self.assertIn(
            "PRECOMPACT_USER_DETAIL",
            self.service.task_read(task["id"])["findings"],
        )
        with self.service.vault.diagnostics._connect() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(str(transcript), dump)
        self.assertNotIn("Bearer_SYNTHETIC_SECRET", dump)
        self.assertNotIn("Bearer_SYNTHETIC_SECRET", self.service.task_read(task["id"])["findings"])
        self.assertEqual(self.hook("UserPromptSubmit", prompt="already restored"), {})
        latest = self.service.task_read(task["id"])
        self.hook("PreCompact", trigger="auto", transcript_path=str(transcript))
        self.assertEqual(self.service.task_read(task["id"])["revision"], latest["revision"] + 1)

    def test_compaction_capture_and_consume_do_not_start_search(self):
        task = self.task()
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "without-index", "cwd": str(self.root), "trigger": "auto"}
        with patch.object(self.service, "start", side_effect=AssertionError("search unavailable")):
            for event in ("UserPromptSubmit", "SessionStart"):
                with self.subTest(event=event):
                    self.assertEqual(handler.handle("PreCompact", payload), {})
                    restored = handler.handle(event, {**payload, "prompt": "continue", "source": "compact"})
                    self.assertIn(task["id"], restored["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.service.task_read(task["id"])["revision"], 3)

    def test_compaction_persist_retries_once_when_task_moved_on(self):
        task = self.task()
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        stale = self.service.task_read(task["id"], checkpoint_limit=1)
        self.call("task_checkpoint", task_id=task["id"], expected_revision=stale["revision"],
                  expected_etag=stale["etag"], findings="moved on", progress="p",
                  next_step="n", operation_id="moved")
        handler.persist_compaction((stale, "SNAPSHOT_AFTER_CONFLICT"), {"session_id": "s", "trigger": "auto"})
        latest = self.service.task_read(task["id"])
        self.assertIn("SNAPSHOT_AFTER_CONFLICT", latest["findings"])
        self.assertIn("moved on", latest["findings"])

    def test_compaction_restores_latest_correction_instead_of_captured_conclusion(self):
        task = self.task()
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=1,
            expected_etag=task["etag"], findings="OBSOLETE_BUILD_CAUSE",
            progress="Old investigation", next_step="OBSOLETE_ROLLBACK",
            operation_id="old-conclusion",
        )
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "fresh-correction", "cwd": str(self.root), "trigger": "auto"}
        transcript = Path(self.temporary.name) / "old-assistant.jsonl"
        transcript.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "OBSOLETE_ASSISTANT_ADVICE"}],
        }}), encoding="utf-8")
        handler.handle("PreCompact", {**payload, "transcript_path": str(transcript)})
        latest = self.service.task_read(task["id"])
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=latest["revision"],
            expected_etag=latest["etag"], findings="Same build works; old attribution withdrawn.",
            progress="NEW_CORRECTION", next_step="COMPARE_CURRENT_REQUESTS",
            operation_id="correct-conclusion",
        )
        with patch.object(self.service, "start", side_effect=AssertionError("index must stay stopped")):
            restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NEW_CORRECTION", text)
        self.assertIn("COMPARE_CURRENT_REQUESTS", text)
        self.assertNotIn("OBSOLETE_BUILD_CAUSE", text)
        self.assertNotIn("OBSOLETE_ROLLBACK", text)
        self.assertNotIn("OBSOLETE_ASSISTANT_ADVICE", text)
        self.assertEqual(handler.handle("UserPromptSubmit", payload), {})

    def test_compaction_does_not_restore_completed_task_after_queue_promotion(self):
        first = self.task()
        second = self.call(
            "task_open", project_id=self.project["id"], title="New current task",
            goal="NEW_GOAL", acceptance=["new acceptance"], operation_id="next-task",
        )
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "task-transition", "cwd": str(self.root), "trigger": "auto"}
        handler.handle("PreCompact", payload)
        latest = self.service.task_read(first["id"])
        self.call(
            "task_close", task_id=first["id"], expected_revision=latest["revision"],
            expected_etag=latest["etag"], outcome="completed", summary="Old task complete",
            acceptance_results={"regression passes": "passed"}, operation_id="complete-old",
        )
        restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue new task"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn(second["id"], text)
        self.assertIn("NEW_GOAL", text)
        self.assertNotIn(f"Task [{first['id']}]", text)

    def test_compaction_read_failure_does_not_replay_unverified_old_task(self):
        task = self.task()
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "read-failure", "cwd": str(self.root), "trigger": "auto"}
        handler.handle("PreCompact", payload)
        with patch.object(self.service, "task_read", side_effect=DiagnosticError("RECORD_INVALID", "unreadable")):
            restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(task["id"], text)
        self.assertIn("could not be verified", text)

    def test_compaction_rebinds_missing_task_and_does_not_replay_legacy_snapshot(self):
        task = self.task()
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "legacy-snapshot", "cwd": str(self.root), "trigger": "auto"}
        with handler.session("codex:legacy-snapshot", str(self.root)) as state:
            state["task_id"] = "task_missing"
        handler.handle("PreCompact", payload)
        with handler.session("codex:legacy-snapshot", str(self.root)) as state:
            self.assertEqual(state["task_id"], task["id"])
            state["compaction_snapshot"] = "UNVERIFIED_LEGACY_CONCLUSION"
            state.pop("compaction_task_fingerprint", None)
            state.pop("compaction_details", None)
        restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn(task["id"], text)
        self.assertNotIn("UNVERIFIED_LEGACY_CONCLUSION", text)

    def test_compaction_ignores_foreign_project_task_binding(self):
        task = self.task()
        foreign_root = Path(self.temporary.name) / "foreign"
        foreign_root.mkdir()
        project = self.call(
            "project_register", root=str(foreign_root), name="Foreign project",
            operation_id="foreign-project",
        )
        foreign_task = self.call(
            "task_open", project_id=project["id"], title="Foreign task", goal="FOREIGN_GOAL",
            acceptance=["foreign acceptance"], operation_id="foreign-task",
        )
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "foreign-binding", "cwd": str(self.root), "trigger": "auto"}
        with handler.session("codex:foreign-binding", str(self.root)) as state:
            state["task_id"] = foreign_task["id"]
        handler.handle("PreCompact", payload)
        restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn(task["id"], text)
        self.assertNotIn(foreign_task["id"], text)
        self.assertNotIn("FOREIGN_GOAL", text)
        self.assertEqual(self.service.task_read(foreign_task["id"])["revision"], 1)

    def test_transcript_details_skip_internal_messages_but_keep_human_request(self):
        transcript = Path(self.temporary.name) / "internal-messages.jsonl"
        messages = [
            "Keep the original native model requirements.",
            "<codex_internal_context source=\"goal\">INTERNAL_GOAL</codex_internal_context>",
            "<environment_context>ENVIRONMENT_BLOCK</environment_context>",
            "# AGENTS.md instructions for C:/project\nAGENT_RULES",
            "<hook_prompt>HOOK_MAINTENANCE</hook_prompt>",
            "<turn_aborted>INTERRUPTION</turn_aborted>",
        ]
        records = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": message}],
            }} for message in messages
        ]
        records.append({"type": "event_msg", "payload": {
            "type": "user_message", "message": messages[1],
        }})
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8",
        )
        self.assertEqual(_transcript_details(str(transcript))["user"], messages[0])

    def test_transcript_details_extract_clipboard_request_and_skip_noise(self):
        transcript = Path(self.temporary.name) / "clipboard-messages.jsonl"
        records = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        "# Files mentioned by the user:\n\n"
                        "## screenshot.png: C:/tmp/screenshot.png\nImage attachment: true\n\n"
                        "## My request:\n请修复恢复后的用户意图\n"
                        '<image name=[Image #1] path="C:/tmp/screenshot.png">'
                        "</image>"
                    ),
                }],
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "<codex_internal_context source=\"goal\">INTERNAL_GOAL</codex_internal_context>",
                }],
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "# Files mentioned by the user:\n\n## orphan.png",
                }],
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "The following is the Codex agent history added since your last approval",
                }],
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ASSISTANT_DETAIL"}],
            }},
        ]
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8",
        )
        details = _transcript_details(str(transcript))
        self.assertEqual(details["user"], "请修复恢复后的用户意图")
        self.assertEqual(details["assistant"], "ASSISTANT_DETAIL")

    def test_transcript_details_keep_request_after_agents_instructions(self):
        transcript = Path(self.temporary.name) / "agents-with-request.jsonl"
        records = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        "# AGENTS.md instructions for C:/project\n\n"
                        "<INSTRUCTIONS>\nAGENT_RULES\n</INSTRUCTIONS>\n\n"
                        "继续修复恢复后的真实用户请求"
                    ),
                }],
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ASSISTANT_DETAIL"}],
            }},
        ]
        transcript.write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8",
        )
        details = _transcript_details(str(transcript))
        self.assertEqual(details["user"], "继续修复恢复后的真实用户请求")
        self.assertEqual(details["assistant"], "ASSISTANT_DETAIL")

    def test_session_end_discards_pending_compaction(self):
        self.task()
        self.hook("PreCompact", trigger="auto")
        self.hook("Interrupt")
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.hook("UserPromptSubmit", prompt="continue"), {})
        self.assertEqual(self.service.vault.diagnostics.hook_state("codex:codex-session"), {})

    def test_precompact_checkpoint_is_recorded_under_a_live_foreign_claim(self):
        task = self.task()
        self.service.start()
        self.addCleanup(self.service.close)
        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["state"], "ready")
        claim = prepared["execution"]
        self.assertEqual(claim["task_id"], task["id"])

        transcript = Path(self.temporary.name) / "live-claim-transcript.jsonl"
        transcript.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "PRECOMPACT_UNDER_LIVE_CLAIM"}],
        }}), encoding="utf-8")
        self.assertEqual(
            self.hook("PreCompact", trigger="auto", transcript_path=str(transcript)),
            {},
        )

        # The pre-compaction snapshot reaches the task record even though the
        # resident MCP service still owns the execution claim.
        latest = self.service.task_read(task["id"])
        self.assertEqual(latest["revision"], task["revision"] + 1)
        self.assertIn("[Pre-compaction snapshot]", latest["findings"])
        self.assertIn("PRECOMPACT_UNDER_LIVE_CLAIM", latest["findings"])
        self.assertEqual(latest["checkpoint_total"], 1)

        # Recording the snapshot must not acquire, release or steal the claim.
        held = self.service.execution.current(task["id"])
        self.assertEqual(held["claim_id"], claim["claim_id"])
        self.assertTrue(self.service.execution.is_authorized(task["id"]))
        foreign = Service(self.home)
        self.addCleanup(foreign.close)
        self.assertFalse(foreign.execution.is_authorized(task["id"]))

        # The resident driver keeps ownership and continues from the new revision.
        continued = self.call(
            "task_checkpoint", task_id=task["id"],
            expected_revision=latest["revision"], expected_etag=latest["etag"],
            findings="Driver continued after the hook write.",
            progress="Hook snapshot integrated", next_step="NEXT_STEP_AFTER_HOOK_WRITE",
            operation_id="driver-after-hook",
        )
        self.assertGreater(continued["revision"], latest["revision"])
        current = self.service.task_read(task["id"])
        self.assertEqual(current["next_step"], "NEXT_STEP_AFTER_HOOK_WRITE")
        self.assertEqual(self.service.execution.current(task["id"])["claim_id"], claim["claim_id"])

    def test_precompact_checkpoint_failure_is_observable_and_keeps_snapshot(self):
        task = self.task()
        handler = CodexHooks(self.service.vault.diagnostics, self.root, service=self.service)
        payload = {"session_id": "checkpoint-failure", "cwd": str(self.root), "trigger": "auto"}
        with patch.object(
            self.service, "task_compaction_snapshot",
            side_effect=DiagnosticError("STORAGE_ERROR", "Snapshot storage unavailable."),
        ):
            self.assertEqual(handler.handle("PreCompact", payload), {})

        state = self.service.vault.diagnostics.hook_state("codex:checkpoint-failure")
        self.assertTrue(state["compaction_pending"])
        self.assertEqual(state["compaction_checkpoint"]["state"], "failed")
        self.assertEqual(state["compaction_checkpoint"]["code"], "STORAGE_ERROR")
        self.assertEqual(self.service.task_read(task["id"])["revision"], task["revision"])

        restored = handler.handle("UserPromptSubmit", {**payload, "prompt": "continue"})
        text = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not receive a pre-compaction checkpoint", text)
        self.assertIn("STORAGE_ERROR", text)
        self.assertIn(task["id"], text)
