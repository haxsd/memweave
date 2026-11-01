import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.services import Service


class LongRunningHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "home"
        self.service = Service(self.home)
        self.service.start()
        self.addCleanup(self.service.close)
        self.project = self.call(
            "project_register", root=str(self.root), name="长期项目", operation_id="project"
        )["id"]

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def open_task(self, title, operation_id):
        return self.call(
            "task_open",
            project_id=self.project,
            title=title,
            goal=f"{title} 的目标",
            acceptance=[f"{title} 的验收"],
            operation_id=operation_id,
        )

    def archive_current_task(self, task):
        current = self.call("task_read", task_id=task["id"])
        closed = self.call(
            "task_close",
            task_id=current["id"],
            expected_revision=current["revision"],
            expected_etag=current["etag"],
            outcome="completed",
            summary="批次完成",
            acceptance_results={current["acceptance"][0]: "已验证"},
            operation_id=f"close-{task['id']}",
        )
        review = self.call("memory_review", task_id=task["id"])
        self.call(
            "memory_review_complete",
            task_id=task["id"],
            expected_revision=review["task"]["revision"],
            expected_etag=review["task"]["etag"],
            memory_ids=[],
            summary="本批次没有可复用知识",
            operation_id=f"review-{task['id']}",
        )
        return closed, current

    def test_bounded_task_pages_and_explicit_archived_history(self):
        tasks = [
            self.open_task(
                "历史批次零" if position == 0 else f"批次任务 {position}",
                f"task-{position}",
            )
            for position in range(25)
        ]
        page = self.call(
            "task_list", project_id=self.project, limit=10
        )
        self.assertEqual(page["status"], "open")
        self.assertEqual(page["total"], 25)
        self.assertEqual(len(page["items"]), 10)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 10)
        self.assertEqual(page["counts"]["current"], 1)
        self.assertEqual(page["counts"]["queued"], 24)
        self.assertEqual(page["counts"]["archived"], 0)
        self.assertEqual(
            [item["id"] for item in page["items"]],
            [task["id"] for task in tasks[:10]],
        )

        self.archive_current_task(tasks[0])
        archived = self.call(
            "task_list", project_id=self.project, status="archived", query="历史批次零"
        )
        self.assertEqual(archived["total"], 1)
        self.assertEqual(archived["items"][0]["id"], tasks[0]["id"])
        self.assertEqual(archived["items"][0]["lifecycle"], "archived")
        self.assertIsNotNone(archived["items"][0]["archived_at"])

        current = self.call("task_list", project_id=self.project, limit=5)
        self.assertEqual(current["total"], 24)
        self.assertEqual(current["counts"]["archived"], 1)
        self.assertNotIn(tasks[0]["id"], {item["id"] for item in current["items"]})

        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["task_id"], tasks[1]["id"])
        self.assertEqual(prepared["task_counts"]["total"], 25)
        self.assertEqual(prepared["task_counts"]["archived"], 1)
        self.assertEqual(len(prepared["tasks"]), 20)
        self.assertTrue(prepared["tasks_truncated"])
        self.assertEqual([item["id"] for item in prepared["history"]], [tasks[0]["id"]])
        self.assertNotIn("历史批次零", prepared["context"]["text"])

    def test_checkpoints_are_individual_records_with_bounded_pages(self):
        task = self.open_task("检查点批次", "checkpoint-task")
        initial = self.call("task_read", task_id=task["id"])
        revision, etag = initial["revision"], initial["etag"]
        for position in range(25):
            saved = self.call(
                "task_checkpoint",
                task_id=task["id"],
                expected_revision=revision,
                expected_etag=etag,
                findings=f"finding-{position}",
                progress=f"progress-{position}",
                next_step=f"next-{position}",
                operation_id=f"checkpoint-{position}",
            )
            revision, etag = saved["revision"], saved["etag"]

        latest = self.call("task_read", task_id=task["id"])
        self.assertEqual(latest["checkpoint_total"], 25)
        self.assertEqual(len(latest["checkpoints"]), 10)
        self.assertTrue(latest["checkpoints_truncated"])
        self.assertEqual(
            [item["sequence"] for item in latest["checkpoints"]],
            list(range(16, 26)),
        )
        self.assertEqual(latest["body"], initial["body"])
        self.assertNotIn("finding-24", latest["body"])

        middle = self.call(
            "task_read",
            task_id=task["id"],
            checkpoint_offset=10,
            checkpoint_limit=10,
        )
        self.assertEqual([item["sequence"] for item in middle["checkpoints"]], list(range(6, 16)))
        self.assertEqual(middle["next_checkpoint_offset"], 20)
        oldest = self.call(
            "task_read",
            task_id=task["id"],
            checkpoint_offset=20,
            checkpoint_limit=10,
        )
        self.assertEqual([item["sequence"] for item in oldest["checkpoints"]], list(range(1, 6)))
        self.assertIsNone(oldest["next_checkpoint_offset"])
        self.assertEqual(
            len(list((self.home / "checkpoints").glob("checkpoint_*.md"))),
            25,
        )

    def test_phase_closure_does_not_append_to_task_body(self):
        task = self.open_task("阶段批次", "phase-task")
        initial = self.call("task_read", task_id=task["id"])
        first = self.call(
            "task_phase_close",
            task_id=task["id"],
            expected_revision=initial["revision"],
            expected_etag=initial["etag"],
            title="需求批次一",
            summary="第一批需求完成",
            next_step="等待下一批需求",
            operation_id="phase-1",
        )
        second = self.call(
            "task_phase_close",
            task_id=task["id"],
            expected_revision=first["task_revision"],
            expected_etag=first["task_etag"],
            title="需求批次二",
            summary="几个月后第二次需求完成",
            next_step="继续观察",
            operation_id="phase-2",
        )
        latest = self.call("task_read", task_id=task["id"])
        self.assertEqual(latest["body"], initial["body"])
        self.assertEqual(latest["phase_total"], 2)
        self.assertEqual(
            [item["title"] for item in latest["phases"]],
            ["需求批次一", "需求批次二"],
        )
        self.assertEqual(latest["revision"], second["task_revision"])
        with self.assertRaises(DiagnosticError):
            self.call("task_list", project_id=self.project, limit=101)


if __name__ == "__main__":
    unittest.main()
