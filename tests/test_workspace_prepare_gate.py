import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.services import Service


class WorkspacePrepareGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.service = Service(Path(self.temporary.name) / "home")
        self.service.require_workspace_prepare = True
        self.service.start()
        self.addCleanup(self.service.close)
        self.project = self.call(
            "project_register", root=str(self.root), name="门禁", operation_id="project"
        )["id"]

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def test_status_and_register_remain_available_before_prepare(self):
        status = self.call("status")
        self.assertTrue(status["initialized"])
        listed = self.call("project_list")
        self.assertEqual(listed["items"][0]["id"], self.project)

    def test_project_tools_refuse_until_workspace_prepare(self):
        with self.assertRaises(DiagnosticError) as raised:
            self.call(
                "task_open",
                project_id=self.project,
                title="先开门禁",
                goal="验证硬约定",
                acceptance=["prepare 后再开"],
                operation_id="blocked",
            )
        self.assertEqual(raised.exception.code, "PREPARE_REQUIRED")

        prepared = self.call("workspace_prepare", root=str(self.root), intent="inspect")
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(prepared["project"]["id"], self.project)

        task = self.call(
            "task_open",
            project_id=self.project,
            title="已准备",
            goal="验证硬约定",
            acceptance=["通过"],
            operation_id="opened",
        )
        self.assertEqual(task["status"], "current")

    def test_prepare_is_per_project_and_process_local(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        other_id = self.call(
            "project_register", root=str(other), name="另一项目", operation_id="other"
        )["id"]
        self.call("workspace_prepare", root=str(self.root), intent="inspect")
        with self.assertRaises(DiagnosticError) as raised:
            self.call(
                "task_open",
                project_id=other_id,
                title="未准备的项目",
                goal="应拒绝",
                acceptance=["拒绝"],
                operation_id="other-task",
            )
        self.assertEqual(raised.exception.code, "PREPARE_REQUIRED")
        self.call("workspace_prepare", root=str(other), intent="inspect")
        opened = self.call(
            "task_open",
            project_id=other_id,
            title="另一项目任务",
            goal="已准备",
            acceptance=["通过"],
            operation_id="other-ready",
        )
        self.assertEqual(opened["status"], "current")

    def test_registration_required_does_not_unlock_project_tools(self):
        unknown = Path(self.temporary.name) / "unknown"
        unknown.mkdir()
        prepared = self.call("workspace_prepare", root=str(unknown), intent="inspect")
        self.assertEqual(prepared["state"], "registration_required")
        with self.assertRaises(DiagnosticError) as raised:
            self.call(
                "task_open",
                project_id=self.project,
                title="仍未解锁",
                goal="registration_required 不算成功",
                acceptance=["仍拒绝"],
                operation_id="still-blocked",
            )
        self.assertEqual(raised.exception.code, "PREPARE_REQUIRED")

    def test_gate_off_keeps_cli_and_tests_unblocked(self):
        plain = Service(Path(self.temporary.name) / "plain-home")
        plain.start()
        self.addCleanup(plain.close)
        root = Path(self.temporary.name) / "plain-root"
        root.mkdir()
        project = plain.dispatch(
            "project_register",
            {"root": str(root), "name": "无门禁", "operation_id": "plain"},
        )["data"]
        task = plain.dispatch(
            "task_open",
            {
                "project_id": project["id"],
                "title": "直开",
                "goal": "默认关闭硬门",
                "acceptance": ["通过"],
                "operation_id": "plain-task",
            },
        )["data"]
        self.assertEqual(task["status"], "current")


if __name__ == "__main__":
    unittest.main()
