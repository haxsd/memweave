import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from memweave.diagnostics import DiagnosticError
from memweave.services import Service
from memweave.storage import Vault, encode_record, now


class TaskQueueTests(unittest.TestCase):
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
            "project_register", root=str(self.root), name="队列测试", operation_id="project"
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

    def test_single_current_queue_promotes_in_order(self):
        first = self.open_task("第一项", "task-1")
        second = self.open_task("第二项", "task-2")
        third = self.open_task("第三项", "task-3")
        self.assertEqual([first["status"], second["status"], third["status"]],
                         ["current", "queued", "queued"])
        queued = self.call("task_list", project_id=self.project, status="queued")["items"]
        self.assertEqual([item["id"] for item in queued], [second["id"], third["id"]])

        closed = self.call(
            "task_close",
            task_id=first["id"],
            expected_revision=first["revision"],
            expected_etag=first["etag"],
            outcome="completed",
            summary="第一项完成",
            acceptance_results={"第一项 的验收": "已运行验证"},
            operation_id="close-1",
        )
        self.assertEqual(closed["promoted_task_id"], second["id"])
        promoted = self.call("task_read", task_id=second["id"])
        self.assertEqual(promoted["status"], "current")
        self.assertEqual(self.call("task_read", task_id=third["id"])["status"], "queued")

        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(prepared["task_id"], second["id"])
        self.assertEqual([item["id"] for item in prepared["tasks"]],
                         [second["id"], third["id"]])
        with self.assertRaises(DiagnosticError) as caught:
            self.call("workspace_prepare", root=str(self.root), intent="resume",
                      task_id=third["id"])
        self.assertEqual(caught.exception.code, "TASK_NOT_CURRENT")

    def test_concurrent_task_open_creates_exactly_one_current(self):
        services = [Service(self.home), Service(self.home)]

        def create(position):
            return services[position].dispatch("task_open", {
                "project_id": self.project,
                "title": f"并发任务 {position}",
                "goal": "只能有一个当前任务",
                "acceptance": ["唯一 current"],
                "operation_id": f"concurrent-{position}",
            })["data"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(create, (0, 1)))
        self.assertCountEqual([item["status"] for item in created], ["current", "queued"])
        tasks = self.call("task_list", project_id=self.project)["items"]
        self.assertEqual(sum(item["status"] == "current" for item in tasks), 1)
        self.assertEqual(sum(item["status"] == "queued" for item in tasks), 1)

    def test_legacy_active_tasks_migrate_to_one_current_and_queue(self):
        self.service.close()
        vault = Vault(self.home)
        project_id = "project_legacy"
        created = "2026-10-27T00:00:00+00:00"
        project = {
            "id": project_id, "type": "project", "scope": "project", "project_id": project_id,
            "title": "旧项目", "name": "旧项目", "root": str(self.root), "status": "active",
            "body": "# 旧项目\n", "revision": 1, "created_at": created, "updated_at": created,
        }
        vault.commit("legacy-project", {"project": project_id}, lambda: (
            [{"record": project, "path": f"projects/{project_id}.md", "expected": None}],
            {"id": project_id},
        ))
        for position, title in enumerate(("旧任务甲", "旧任务乙"), start=1):
            task_id = f"task_legacy_{position}"
            task = {
                "id": task_id, "type": "task", "scope": "project", "project_id": project_id,
                "title": title, "goal": "旧目标", "acceptance": ["旧验收"], "status": "active",
                "findings": "", "progress": "", "next_step": "", "body": f"# {title}\n",
                "revision": 1, "created_at": f"2026-10-27T00:0{position}:00+00:00",
                "updated_at": f"2026-10-27T00:0{position}:00+00:00",
            }
            vault.commit(f"legacy-{position}", {"task": task_id}, lambda task=task: (
                [{"record": task, "path": f"tasks/{task['id']}.md", "expected": None}],
                {"id": task["id"]},
            ))

        with Service(self.home) as migrated:
            tasks = migrated.dispatch(
                "task_list", {"project_id": project_id, "status": "all"}
            )["data"]["items"]
            self.assertEqual([item["status"] for item in tasks], ["current", "queued"])
            self.assertEqual([item["queue_position"] for item in tasks], [0, 1])
            self.assertEqual(tasks[0]["id"], "task_legacy_1")

        with Service(self.home) as restarted:
            again = restarted.dispatch(
                "task_list", {"project_id": project_id, "status": "all"}
            )["data"]["items"]
            self.assertEqual([item["revision"] for item in again], [2, 2])

    def test_legacy_migration_resolves_duplicate_current_and_preserves_closed_tasks(self):
        self.service.close()
        vault = Vault(self.home)
        project_id = "project_conflict"
        created = "2026-10-27T00:00:00+00:00"
        project = {
            "id": project_id, "type": "project", "scope": "project", "project_id": project_id,
            "title": "冲突项目", "name": "冲突项目", "root": str(self.root), "status": "active",
            "body": "# 冲突项目\n", "revision": 1, "created_at": created, "updated_at": created,
        }
        vault.commit("conflict-project", {"project": project_id}, lambda: (
            [{"record": project, "path": f"projects/{project_id}.md", "expected": None}],
            {"id": project_id},
        ))
        task_specs = (
            ("task_conflict_current_early", "current", 4, "2026-10-27T00:01:00+00:00"),
            ("task_conflict_current_late", "current", 9, "2026-10-27T00:02:00+00:00"),
            ("task_conflict_legacy", "active", 0, "2026-10-26T23:59:00+00:00"),
            ("task_conflict_completed", "completed", 2, "2026-10-26T20:00:00+00:00"),
            ("task_conflict_cancelled", "cancelled", 3, "2026-10-26T21:00:00+00:00"),
        )
        for task_id, status, position, timestamp in task_specs:
            task = {
                "id": task_id, "type": "task", "scope": "project", "project_id": project_id,
                "title": task_id, "goal": "迁移目标", "acceptance": ["保留状态"],
                "status": status, "queue_position": position, "findings": "", "progress": "",
                "next_step": "", "body": f"# {task_id}\n", "revision": 1,
                "created_at": timestamp, "updated_at": timestamp,
            }
            vault.commit(f"create-{task_id}", {"task": task_id}, lambda task=task: (
                [{"record": task, "path": f"tasks/{task['id']}.md", "expected": None}],
                {"id": task["id"]},
            ))

        with Service(self.home) as migrated:
            tasks = migrated.dispatch(
                "task_list", {"project_id": project_id, "status": "all"}
            )["data"]["items"]
        by_id = {task["id"]: task for task in tasks}
        self.assertEqual(by_id["task_conflict_current_early"]["status"], "current")
        self.assertEqual(by_id["task_conflict_current_late"]["status"], "queued")
        self.assertEqual(by_id["task_conflict_legacy"]["status"], "queued")
        self.assertEqual(by_id["task_conflict_completed"]["status"], "completed")
        self.assertEqual(by_id["task_conflict_cancelled"]["status"], "cancelled")
        self.assertEqual(by_id["task_conflict_completed"]["revision"], 1)
        self.assertEqual(by_id["task_conflict_cancelled"]["revision"], 1)
        self.assertEqual(
            sorted(
                task["queue_position"]
                for task in tasks
                if task["status"] in ("current", "queued")
            ),
            [0, 4, 9],
        )

    def test_parallel_work_units_block_parent_close_until_resolved(self):
        task = self.open_task("并行任务", "parallel-task")
        first = self.call(
            "work_unit_update",
            task_id=task["id"],
            title="后端实现",
            owner="worker-backend",
            status="running",
            progress="正在实现接口",
            next_step="运行后端测试",
            operation_id="unit-1",
        )
        second = self.call(
            "work_unit_update",
            task_id=task["id"],
            title="前端实现",
            owner="worker-frontend",
            status="running",
            progress="正在调整页面",
            next_step="运行页面检查",
            operation_id="unit-2",
        )
        detail = self.call("task_read", task_id=task["id"])
        self.assertEqual({item["status"] for item in detail["work_units"]}, {"running"})
        self.assertEqual({item["owner"] for item in detail["work_units"]},
                         {"worker-backend", "worker-frontend"})

        self.call(
            "work_unit_update",
            unit_id=first["id"],
            expected_revision=first["revision"],
            expected_etag=first["etag"],
            status="completed",
            progress="后端测试通过",
            verification="unit tests passed",
            operation_id="unit-1-complete",
        )
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "task_close",
                task_id=task["id"],
                expected_revision=task["revision"],
                expected_etag=task["etag"],
                outcome="completed",
                summary="并行任务完成",
                acceptance_results={"并行任务 的验收": "测试通过"},
                operation_id="close-blocked",
            )
        self.assertEqual(caught.exception.code, "WORK_UNITS_ACTIVE")

        blocked = self.call(
            "work_unit_update",
            unit_id=second["id"],
            expected_revision=second["revision"],
            expected_etag=second["etag"],
            status="blocked",
            progress="等待后端接口",
            blocker="接口契约未确认",
            next_step="确认契约",
            operation_id="unit-2-blocked",
        )
        context = self.call("context_get", project_id=self.project, task_id=task["id"],
                            budget=4000)
        self.assertIn("worker-frontend", context["text"])
        self.assertIn("接口契约未确认", context["text"])

        closed = self.call(
            "task_close",
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            outcome="completed",
            summary="并行任务完成",
            acceptance_results={"并行任务 的验收": "测试通过"},
            abandon_work_units=True,
            operation_id="close-abandon",
        )
        self.assertEqual(closed["abandoned_work_units"], [blocked["id"]])
        final_units = {
            unit["id"]: unit["status"]
            for unit in self.call("task_read", task_id=task["id"])["work_units"]
        }
        self.assertEqual(final_units[first["id"]], "completed")
        self.assertEqual(final_units[second["id"]], "cancelled")

    def test_subagents_update_distinct_work_units_without_parent_conflict(self):
        task = self.open_task("并发更新", "concurrent-task")
        workers = [Service(self.home), Service(self.home)]
        self.addCleanup(workers[0].close)
        self.addCleanup(workers[1].close)
        units = [
            self.call(
                "work_unit_update", task_id=task["id"], title=f"工作 {index}",
                owner=f"worker-{index}", status="running", progress=f"worker-{index} 已分派",
                operation_id=f"unit-{index}",
            )
            for index in (1, 2)
        ]

        def update(pair):
            position, unit = pair
            return workers[position].dispatch("work_unit_update", {
                "unit_id": unit["id"],
                "expected_revision": unit["revision"],
                "expected_etag": unit["etag"],
                "status": "completed",
                "progress": f"{unit['owner']} 完成",
                "operation_id": f"complete-{unit['owner']}",
            })["data"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, enumerate(units)))
        self.assertEqual({item["status"] for item in results}, {"completed"})
        self.assertEqual(self.call("task_read", task_id=task["id"])["revision"], 1)

    def test_phase_closes_and_reviews_without_closing_current_task(self):
        task = self.open_task("阶段任务", "phase-task")
        phase = self.call(
            "task_phase_close",
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            title="需求与设计",
            summary="设计边界已确认",
            findings="设计采用单 current 加并行 WorkUnit",
            verification="设计评审完成",
            next_step="进入实现阶段",
            operation_id="phase-close",
        )
        current = self.call("task_read", task_id=task["id"])
        self.assertEqual(current["status"], "current")
        self.assertEqual(current["phases"][0]["id"], phase["phase_id"])
        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual([item["id"] for item in prepared["pending_phases"]], [phase["phase_id"]])

        review = self.call("memory_review", task_id=task["id"], phase_id=phase["phase_id"])
        self.assertEqual(review["state"], "pending")
        memory = self.call(
            "memory_write", project_id=self.project, title="阶段设计结论",
            body="单 current 任务内允许并行 WorkUnit。",
            sources=[{"type": "task", "reference": task["id"]}],
            status="candidate", operation_id="phase-memory",
        )
        self.call(
            "memory_review_complete",
            task_id=task["id"],
            phase_id=phase["phase_id"],
            expected_revision=review["phase"]["revision"],
            expected_etag=review["phase"]["etag"],
            memory_ids=[memory["id"]],
            summary="阶段经验经审查后保留为候选",
            operation_id="phase-review-complete",
        )
        reviewed = self.call("memory_review", task_id=task["id"], phase_id=phase["phase_id"])
        self.assertEqual(reviewed["state"], "reviewed")
        self.assertEqual(self.call("task_read", task_id=task["id"])["status"], "current")

    def test_checkpoint_persists_failures_and_verification(self):
        task = self.open_task("验证记录", "verification-task")
        self.call(
            "task_checkpoint",
            task_id=task["id"],
            expected_revision=task["revision"],
            expected_etag=task["etag"],
            findings="定位到旧状态模型",
            progress="迁移实现中",
            failures="第一次尝试在锁内启动索引导致自锁",
            verification="迁移单测通过",
            next_step="运行全套测试",
            operation_id="verification-checkpoint",
        )
        restored = self.call("task_read", task_id=task["id"])
        self.assertEqual(restored["failures"], "第一次尝试在锁内启动索引导致自锁")
        self.assertEqual(restored["verification"], "迁移单测通过")
        self.assertEqual(restored["checkpoint_total"], 1)
        self.assertEqual(
            restored["checkpoints"][0]["failures"],
            "第一次尝试在锁内启动索引导致自锁",
        )
        self.assertEqual(restored["checkpoints"][0]["verification"], "迁移单测通过")
        self.assertNotIn("### Failures", restored["body"])

    def test_execution_claim_blocks_second_session_and_releases(self):
        task = self.open_task("主任务", "claim-main")
        owner = Service(self.home)
        owner.start()
        self.addCleanup(owner.close)
        claimed = owner.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
        })["data"]
        self.assertEqual(claimed["state"], "ready")
        self.assertEqual(claimed["execution"]["state"], "acquired")
        claim_id = claimed["execution"]["claim_id"]

        other = Service(self.home)
        other.start()
        self.addCleanup(other.close)
        blocked = other.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
        })["data"]
        self.assertEqual(blocked["state"], "task_claimed")
        self.assertIsNone(blocked["task_id"])
        self.assertIsNone(blocked["context"])
        self.assertEqual(blocked["tasks"], [])
        self.assertEqual(blocked["background_task"]["id"], task["id"])
        with self.assertRaises(DiagnosticError) as caught:
            other.dispatch("task_checkpoint", {
                "task_id": task["id"], "expected_revision": task["revision"],
                "expected_etag": task["etag"], "progress": "should not write",
                "next_step": "should not write", "operation_id": "blocked-checkpoint",
            })
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")
        with self.assertRaises(DiagnosticError) as caught:
            other.dispatch("task_phase_close", {
                "task_id": task["id"], "expected_revision": task["revision"],
                "expected_etag": task["etag"], "title": "blocked phase",
                "summary": "should not write", "next_step": "should not write",
                "operation_id": "blocked-phase",
            })
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")
        with self.assertRaises(DiagnosticError) as caught:
            other.dispatch("task_close", {
                "task_id": task["id"], "expected_revision": task["revision"],
                "expected_etag": task["etag"], "outcome": "completed",
                "summary": "should not write",
                "acceptance_results": {"real client works": "should not write"},
                "operation_id": "blocked-close",
            })
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")

        owner.dispatch("task_checkpoint", {
            "task_id": task["id"], "expected_revision": task["revision"],
            "expected_etag": task["etag"], "progress": "owner writes",
            "next_step": "continue", "operation_id": "owner-checkpoint",
        })
        self.assertTrue(claim_id)
        owner.close()

        third = Service(self.home)
        third.start()
        self.addCleanup(third.close)
        resumed = third.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
        })["data"]
        self.assertEqual(resumed["state"], "ready")
        self.assertEqual(resumed["execution"]["state"], "acquired")
        self.assertNotEqual(resumed["execution"]["claim_id"], claim_id)

    def test_side_task_is_isolated_from_main_queue(self):
        main = self.open_task("主开发", "side-main")
        queued = self.open_task("主队列下一项", "side-queued")
        background = self.call("workspace_prepare", root=str(self.root), intent="new")
        self.assertEqual(background["tasks"], [])
        self.assertEqual(background["background_task"]["id"], main["id"])

        side = self.call(
            "task_open", project_id=self.project, title="临时检查",
            goal="不污染主任务", acceptance=["侧任务检查完成"],
            parent_task_id=main["id"], operation_id="side-task",
        )
        self.assertEqual(side["status"], "side")
        self.assertEqual(side["task_kind"], "side")
        self.assertEqual(side["parent_task_id"], main["id"])
        self.assertIsNone(side["queue_position"])

        side_resume = self.call(
            "workspace_prepare", root=str(self.root), intent="resume",
            task_id=side["id"],
        )
        self.assertEqual(side_resume["state"], "ready")
        side_checkpoint = self.call(
            "task_checkpoint", task_id=side["id"], expected_revision=side["revision"],
            expected_etag=side["etag"], progress="侧任务处理中",
            next_step="完成侧任务", operation_id="side-checkpoint",
        )
        side_unit = self.call(
            "work_unit_update", task_id=side["id"], title="侧任务检查",
            owner="side-worker", status="running", progress="检查中",
            operation_id="side-unit",
        )
        self.call(
            "work_unit_update", unit_id=side_unit["id"],
            expected_revision=side_unit["revision"], expected_etag=side_unit["etag"],
            status="completed", progress="检查完成", verification="scope check",
            operation_id="side-unit-complete",
        )
        side_current = self.call("task_read", task_id=side["id"])
        closed = self.call(
            "task_close", task_id=side["id"],
            expected_revision=side_current["revision"], expected_etag=side_current["etag"],
            outcome="completed", summary="侧任务完成",
            acceptance_results={"侧任务检查完成": "已完成且未修改主任务"},
            operation_id="side-close",
        )
        self.assertIsNone(closed["promoted_task_id"])
        self.assertEqual(self.call("task_read", task_id=main["id"])["status"], "current")
        self.assertEqual(self.call("task_read", task_id=queued["id"])["status"], "queued")
        self.assertEqual(self.call("task_read", task_id=side["id"])["status"], "completed")

    def test_delegated_claim_keeps_parallel_work_units_independent(self):
        main = self.open_task("并行主任务", "delegated-main")
        owner = Service(self.home)
        owner.start()
        self.addCleanup(owner.close)
        claimed = owner.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
        })["data"]
        claim_id = claimed["execution"]["claim_id"]
        units = [
            owner.dispatch("work_unit_update", {
                "task_id": main["id"], "title": f"并行单元 {index}",
                "owner": f"worker-{index}", "status": "running",
                "progress": "已分派", "operation_id": f"delegated-unit-{index}",
            })["data"]
            for index in (1, 2)
        ]

        delegate = Service(self.home)
        delegate.start()
        self.addCleanup(delegate.close)
        delegated = delegate.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
            "task_id": main["id"], "execution_claim": claim_id,
        })["data"]
        self.assertEqual(delegated["state"], "ready")
        self.assertEqual(delegated["execution"]["state"], "delegated")

        def complete(pair):
            position, unit = pair
            worker = owner if position == 0 else delegate
            return worker.dispatch("work_unit_update", {
                "unit_id": unit["id"], "expected_revision": unit["revision"],
                "expected_etag": unit["etag"], "status": "completed",
                "progress": "子代理完成", "verification": f"worker-{position + 1}",
                "operation_id": f"delegated-complete-{position}",
            })["data"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            completed = list(pool.map(complete, enumerate(units)))
        self.assertEqual({item["status"] for item in completed}, {"completed"})
        self.assertEqual(self.call("task_read", task_id=main["id"])["revision"], main["revision"])

    def test_existing_work_unit_can_be_updated_by_an_unclaimed_worker(self):
        main = self.open_task("子代理更新", "unclaimed-worker-main")
        owner = Service(self.home)
        owner.start()
        self.addCleanup(owner.close)
        owner.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "resume",
        })
        unit = owner.dispatch("work_unit_update", {
            "task_id": main["id"], "title": "子任务", "owner": "child",
            "status": "running", "progress": "已分派", "operation_id": "unclaimed-unit",
        })["data"]

        child = Service(self.home)
        child.start()
        self.addCleanup(child.close)
        child.dispatch("workspace_prepare", {
            "root": str(self.root), "intent": "inspect",
        })
        completed = child.dispatch("work_unit_update", {
            "unit_id": unit["id"], "expected_revision": unit["revision"],
            "expected_etag": unit["etag"], "status": "completed",
            "progress": "子代理完成", "operation_id": "unclaimed-unit-complete",
        })["data"]
        self.assertEqual(completed["status"], "completed")
        with self.assertRaises(DiagnosticError) as caught:
            child.dispatch("work_unit_update", {
                "task_id": main["id"], "title": "不应创建", "owner": "child",
                "status": "running", "progress": "blocked", "operation_id": "unclaimed-create",
            })
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")

    def test_task_ids_use_local_minute_and_collision_suffix(self):
        with patch("memweave.services._task_timestamp", return_value="20261105-1331"):
            first = self.open_task("时间任务一", "time-task-one")
            second = self.open_task("时间任务二", "time-task-two")
            third = self.open_task("时间任务三", "time-task-three")
        self.assertEqual(first["id"], "task_20261105-1331")
        self.assertEqual(second["id"], "task_20261105-1331-02")
        self.assertEqual(third["id"], "task_20261105-1331-03")
        self.assertTrue((self.home / "tasks" / f"{third['id']}.md").is_file())


if __name__ == "__main__":
    unittest.main()
