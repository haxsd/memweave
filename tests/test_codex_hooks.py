import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memweave.codex_hooks import CodexHooks
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
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".codex").mkdir()
        (nested / ".codex" / "memweave.json").write_text(
            json.dumps({"root": str(nested)}), encoding="utf-8"
        )
        self.task()
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

    def test_session_end_discards_pending_compaction(self):
        self.task()
        self.hook("PreCompact", trigger="auto")
        self.hook("Interrupt")
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.hook("UserPromptSubmit", prompt="continue"), {})
        self.assertEqual(self.service.vault.diagnostics.hook_state("codex:codex-session"), {})
