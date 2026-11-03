import json
import os
import subprocess
import sys
import tempfile
import unittest
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

    def test_start_auto_resumes_unique_task_and_withholds_user_scope(self):
        self.call("memory_write", title="项目约束",
                  body="PROJECT_RULE_VISIBLE 实现修复 OLD_TASK_GOAL_MUST_NOT_LEAK",
                  project_id=self.project["id"],
                  sources=[{"type": "user", "reference": "明确约束"}], operation_id="memory")
        self.call("memory_write", title="用户偏好", body="PRIVATE_USER_PREFERENCE", scope="user",
                  sources=[{"type": "user", "reference": "明确偏好"}], operation_id="user")
        task = self.task()
        response = self.hook("sessionStart")
        context = response.get("additional_context", "")
        self.assertIn("PROJECT_RULE_VISIBLE", context)
        self.assertIn("当前工具列表实际出现 memweave_workspace_prepare", context)
        self.assertIn("未出现时不得凭 Hook 文案发起调用", context)
        self.assertIn("不要用 shell 重读 Skill", context)
        self.assertIn("OLD_TASK_GOAL_MUST_NOT_LEAK", context)
        self.assertIn("唯一 current 任务", context)
        self.assertIn("MemWeave · 任务", context)
        self.assertNotIn("PRIVATE_USER_PREFERENCE", context)
        self.assertLessEqual(len(context), 6000)
        self.assertEqual(self.call("task_read", task_id=task["id"])["revision"], 1)
        state = self.service.vault.diagnostics.hook_state("conversation-1")
        self.assertIs(state["startup_context_emitted"], True)
        self.assertEqual(state["startup_context_source"], "sessionStart")
        self.assertNotIn("context_delivered", state)

    def test_start_without_a_task_still_returns_project_context_and_status_line(self):
        response = self.hook("sessionStart", generation_id="")
        context = response.get("additional_context", "")
        self.assertIn("MEMWEAVE CONTEXT", context)
        self.assertIn("MemWeave · 尚未建立任务", context)

    def test_unknown_or_ambiguous_workspace_never_loads_another_projects_context(self):
        unknown = Path(self.temporary.name) / "unknown"
        unknown.mkdir()
        self.task()
        for roots in ([], [str(unknown)], [str(self.root), str(unknown)], ["."], "invalid"):
            with self.subTest(roots=roots):
                self.assertEqual(self.hook("sessionStart", workspace_roots=roots), {})
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

    def test_queued_task_does_not_replace_current_and_session_end_is_isolated(self):
        first = self.task()
        second = self.task(operation_id="second")
        self.assertEqual(second["status"], "queued")
        for session_id in ("conversation-1", "second"):
            text = self.hook("sessionStart", conversation_id=session_id, session_id=session_id)["additional_context"]
            self.assertIn(first["id"], text)
            self.assertNotIn(second["id"], text)
        self.hook("sessionEnd")
        diagnostics = self.service.vault.diagnostics
        self.assertEqual(diagnostics.hook_state("conversation-1"), {})
        self.assertEqual(diagnostics.hook_state("second")["task_id"], first["id"])


if __name__ == "__main__":
    unittest.main()
