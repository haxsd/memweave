import json
import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.services import Service
from memweave.storage import encode_record


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.service = Service(Path(self.temporary.name) / "home")
        self.service.start()
        self.addCleanup(self.service.close)
        self.project = self.call("project_register", root=str(self.root), name="示例", operation_id="p")["id"]

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def test_progress_only_checkpoint_preserves_findings_failures_and_verification(self):
        task = self.call(
            "task_open", project_id=self.project, title="Preserve investigation",
            goal="Keep verified evidence", acceptance=["real client works"], operation_id="preserve-task",
        )
        checkpoint = self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="ROOT_CAUSE_CONFIRMED",
            failures="KNOWN_LINKER_WORKAROUND", verification="FOCUSED_TEST_PASSED",
            progress="Implementation complete", next_step="Verify real client",
            operation_id="record-evidence",
        )
        parameters = dict(
            task_id=task["id"], expected_revision=checkpoint["revision"],
            expected_etag=checkpoint["etag"], progress="Code pushed", next_step="Observe real client",
            operation_id="progress-only",
        )
        updated = self.call("task_checkpoint", **parameters)
        self.assertEqual(self.call("task_checkpoint", **parameters), updated)
        record = self.call("task_read", task_id=task["id"], checkpoint_limit=1)
        self.assertEqual(record["findings"], "ROOT_CAUSE_CONFIRMED")
        self.assertEqual(record["failures"], "KNOWN_LINKER_WORKAROUND")
        self.assertEqual(record["verification"], "FOCUSED_TEST_PASSED")
        self.assertEqual(record["checkpoints"][0]["findings"], "ROOT_CAUSE_CONFIRMED")
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=updated["revision"],
            expected_etag=updated["etag"], progress="Correction supersedes old evidence",
            findings="", failures="", verification="", next_step="Collect replacement evidence",
            operation_id="explicit-clear",
        )
        cleared = self.call("task_read", task_id=task["id"])
        self.assertEqual(cleared["findings"], "")
        self.assertEqual(cleared["failures"], "")
        self.assertEqual(cleared["verification"], "")

    def test_task_checkpoint_restore_complete_requires_acceptance(self):
        created = self.call("task_open", project_id=self.project, title="认证", goal="实现认证",
                            acceptance=["测试通过"], operation_id="task")
        task = self.call("task_read", task_id=created["id"])
        self.call("task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
                  expected_etag=task["etag"], findings="令牌过期", progress="已定位",
                  next_step="修复刷新逻辑", operation_id="checkpoint")
        restored = Service(self.service.vault.home).dispatch("task_read", {"task_id": task["id"]})["data"]
        self.assertEqual(restored["next_step"], "修复刷新逻辑")
        self.assertEqual(restored["checkpoint_total"], 1)
        self.assertEqual(restored["checkpoints"][0]["findings"], "令牌过期")
        self.assertNotIn("令牌过期", restored["body"])
        self.assertEqual(restored["revision"], 2)
        parameters = dict(task_id=task["id"], expected_revision=2, expected_etag=restored["etag"],
                          outcome="completed", summary="完成", operation_id="close")
        with self.assertRaises(DiagnosticError):
            self.call("task_close", **parameters, acceptance_results={})
        self.call("task_close", **parameters, acceptance_results={"测试通过": "unittest 通过"})
        closed = self.call("task_read", task_id=task["id"])
        self.assertEqual(closed["verification"], "host_reported")
        self.assertEqual(closed["status"], "completed")
        with self.assertRaises(DiagnosticError):
            self.call("task_checkpoint", task_id=task["id"], expected_revision=3,
                      expected_etag=closed["etag"], findings="x", progress="x", next_step="x", operation_id="late")

    def test_scopes_chinese_search_correction_and_forget(self):
        other = self.root.parent / "other"
        other.mkdir()
        other_id = self.call("project_register", root=str(other), name="其他", operation_id="p2")["id"]
        for operation, project, scope, title in [
            ("m1", self.project, "project", "认证服务使用短期令牌"),
            ("m2", other_id, "project", "认证服务其他项目"),
            ("m3", None, "user", "认证服务用户偏好"),
        ]:
            self.call("memory_write", operation_id=operation, project_id=project, scope=scope,
                      title=title, body="刷新令牌机制", kind="fact",
                      sources=[{"type": "user", "reference": "明确输入"}])
        hits = self.call("memory_search", project_id=self.project, query="认证", scope="project")["items"]
        self.assertEqual(len(hits), 1)
        record = hits[0]
        user_hits = self.call("memory_search", project_id=self.project, query="认证", scope="user")["items"]
        self.assertEqual(len(user_hits), 1)
        self.assertEqual(user_hits[0]["scope"], "user")
        self.call("memory_update", memory_id=record["id"], expected_revision=record["revision"],
                  expected_etag=record["etag"], title="认证修订", body="短期令牌五分钟", operation_id="edit")
        updated = self.call("memory_read", memory_id=record["id"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["body"], "短期令牌五分钟")
        self.call("memory_forget", memory_id=record["id"], expected_revision=2,
                  expected_etag=updated["etag"], operation_id="forget")
        self.assertEqual(self.call("memory_search", project_id=self.project, query="", scope="project")["items"], [])
        with self.service.vault.diagnostics._connect() as connection:
            values = str([tuple(row) for row in connection.execute("SELECT * FROM operations")])
            self.assertNotIn("短期令牌五分钟", values)

    def test_user_scope_requires_explicit_user_source(self):
        with self.assertRaises(DiagnosticError) as missing_user:
            self.call(
                "memory_write", scope="user", title="项目事实", body="不应成为跨项目记忆",
                sources=[{"type": "host", "reference": "自动观察"}], operation_id="user-host",
            )
        self.assertEqual(missing_user.exception.code, "SCOPE_DENIED")

    def test_status_reports_knowledge_scope_and_architecture_freshness(self):
        self.call(
            "task_open", project_id=self.project, title="状态计数", goal="验证状态",
            acceptance=["计数正确"], operation_id="status-task",
        )
        self.call(
            "memory_write", project_id=self.project, scope="project", title="项目知识",
            body="项目范围", sources=[{"type": "user", "reference": "输入"}],
            operation_id="status-project-memory",
        )
        self.call(
            "memory_write", scope="user", title="用户偏好", body="长期偏好",
            sources=[{"type": "user", "reference": "明确偏好"}],
            operation_id="status-user-memory",
        )
        source_file = self.root / "main.py"
        source_file.write_text("print('status')\n", encoding="utf-8")
        self.call("project_scan", project_id=self.project, operation_id="status-scan")
        source = self.call("evidence_read", project_id=self.project, path="main.py")
        architecture = self.call(
            "graph_write", project_id=self.project, operation_id="status-architecture",
            title="状态架构", body="入口", sources=[{"path": source["path"], "hash": source["hash"]}],
            relations=[],
        )
        status = self.call("status")
        self.assertEqual(status["knowledge"]["total"], 2)
        self.assertEqual(status["knowledge"]["by_scope"], {"project": 1, "group": 0, "user": 1})
        self.assertEqual(status["knowledge"]["by_status"], {"active": 2, "candidate": 0, "retired": 0})
        self.assertEqual(
            status["knowledge"]["by_scope_status"],
            {
                "project": {"active": 1, "candidate": 0, "retired": 0},
                "group": {"active": 0, "candidate": 0, "retired": 0},
                "user": {"active": 1, "candidate": 0, "retired": 0},
            },
        )
        self.assertEqual(status["architecture"]["total"], 1)
        self.assertEqual(status["architecture"]["stale"], 0)

        other_root = self.root.parent / "status-other"
        other_root.mkdir()
        other_project = self.call(
            "project_register", root=str(other_root), name="其他状态项目",
            operation_id="status-other-project",
        )["id"]
        self.call(
            "memory_write", project_id=other_project, scope="project", title="其他项目知识",
            body="不应计入当前项目", sources=[{"type": "user", "reference": "输入"}],
            operation_id="status-other-memory",
        )
        self.call(
            "task_open", project_id=self.project, title="排队任务", goal="验证项目队列",
            acceptance=["队列计数"], operation_id="status-queued-task",
        )
        project_status = self.call("status", project_id=self.project)
        self.assertEqual(project_status["project"]["id"], self.project)
        self.assertEqual(project_status["project"]["knowledge"]["total"], 1)
        self.assertEqual(
            project_status["project"]["knowledge"]["by_scope"],
            {"project": 1, "group": 0, "user": 0},
        )
        self.assertEqual(
            project_status["project"]["knowledge"]["by_status"],
            {"active": 1, "candidate": 0, "retired": 0},
        )
        self.assertEqual(
            project_status["project"]["tasks"],
            {
                "total": 2, "current": 1, "queued": 1, "side": 0, "pending_review": 0,
                "completed": 0, "cancelled": 0, "archived": 0,
            },
        )
        self.assertEqual(project_status["project"]["architecture"]["total"], 1)
        self.assertEqual(project_status["project"]["architecture"]["stale"], 0)
        other_status = self.call("status", project_id=other_project)
        self.assertEqual(other_status["project"]["knowledge"]["total"], 1)
        self.assertEqual(other_status["project"]["architecture"]["total"], 0)
        with self.assertRaises(DiagnosticError):
            self.call("status", project_id="missing-project")

        source_file.write_text("print('changed')\n", encoding="utf-8")
        stale = self.call("status")
        self.assertEqual(stale["architecture"]["stale"], 1)
        self.assertEqual(stale["architecture"]["stale_record_ids"], [architecture["id"]])
        project_stale = self.call("status", project_id=self.project)
        self.assertEqual(project_stale["project"]["architecture"]["stale"], 1)
        self.assertEqual(
            project_stale["project"]["architecture"]["stale_record_ids"],
            [architecture["id"]],
        )
        self.call(
            "graph_forget", project_id=self.project, operation_id="status-forget-architecture",
            architecture_id=architecture["id"], expected_revision=1,
            expected_etag=architecture["etag"],
        )
        removed = self.call("status")
        self.assertEqual(removed["architecture"]["total"], 0)
        self.assertEqual(removed["architecture"]["stale"], 0)
        project_removed = self.call("status", project_id=self.project)
        self.assertEqual(project_removed["project"]["architecture"]["total"], 0)
        self.assertEqual(project_removed["project"]["architecture"]["stale"], 0)

    def test_retry_registration_and_version_conflict(self):
        retry = self.call("project_register", root=str(self.root), name="示例", operation_id="p")
        self.assertEqual(retry["id"], self.project)
        created = self.call("memory_write", project_id=self.project, scope="project", title="规则",
                            body="内容", sources=[{"type": "user", "reference": "输入"}], operation_id="write")
        memory = self.call("memory_read", memory_id=created["id"])
        with self.assertRaises(DiagnosticError) as caught:
            self.call("memory_update", memory_id=memory["id"], expected_revision=99,
                      expected_etag=memory["etag"], title="x", body="y", operation_id="bad")
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")

    def test_task_scope_is_rejected_and_task_memory_still_works(self):
        task = self.call("task_open", project_id=self.project, title="任务", goal="目标",
                         acceptance=["检查"], operation_id="task")
        with self.assertRaises(DiagnosticError) as caught:
            self.call("memory_write", project_id=self.project, scope="task", title="局部",
                      body="任务专用知识", sources=[{"type": "user", "reference": "输入"}], operation_id="m")
        self.assertEqual(caught.exception.code, "SCOPE_DENIED")
        with self.assertRaises(DiagnosticError) as searched:
            self.call("memory_search", project_id=self.project, scope="task", query="局部")
        self.assertEqual(searched.exception.code, "SCOPE_DENIED")
        latest = self.call("task_read", task_id=task["id"])
        self.call("task_checkpoint", task_id=task["id"], expected_revision=latest["revision"],
                  expected_etag=latest["etag"], findings="信息", progress="进行中",
                  next_step="先验证刷新逻辑", operation_id="task-memory")
        context = self.call("context_get", project_id=self.project, task_id=task["id"], budget=2000)
        self.assertIn("先验证刷新逻辑", context["text"])
        self.assertLessEqual(len(context["text"]), 2000)
        with self.assertRaises(DiagnosticError):
            self.call("context_get", project_id="wrong", task_id=task["id"])

    def test_startup_deletes_legacy_task_scope_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            root = Path(temporary) / "project"
            root.mkdir()
            service = Service(home)
            service.start()
            try:
                project = service.dispatch(
                    "project_register",
                    {"root": str(root), "name": "legacy", "operation_id": "legacy-project"},
                )["data"]["id"]
                task = service.dispatch(
                    "task_open",
                    {
                        "project_id": project,
                        "title": "legacy task",
                        "goal": "goal",
                        "acceptance": ["done"],
                        "operation_id": "legacy-task",
                    },
                )["data"]
                record = {
                    "id": "memory_legacy_task_scope",
                    "type": "memory",
                    "scope": "task",
                    "project_id": project,
                    "task_id": task["id"],
                    "title": "legacy task knowledge",
                    "body": "Only valid for the old task scope.",
                    "revision": 1,
                    "status": "active",
                    "kind": "fact",
                    "sources": [{"type": "task", "reference": task["id"]}],
                    "evidence": [],
                    "created_at": "2026-11-11T00:00:00+00:00",
                    "updated_at": "2026-11-11T00:00:00+00:00",
                }
                target = service.vault.target("knowledge/memory_legacy_task_scope.md")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(encode_record(record), encoding="utf-8")
            finally:
                service.close()

            restarted = Service(home)
            restarted.start()
            try:
                self.assertFalse((home / "knowledge" / "memory_legacy_task_scope.md").exists())
                with self.assertRaises(DiagnosticError):
                    restarted.dispatch(
                        "memory_read", {"memory_id": "memory_legacy_task_scope"},
                    )
                status = restarted.dispatch("status", {})["data"]
                self.assertEqual(status["knowledge"]["by_scope"], {"project": 0, "group": 0, "user": 0})
                with restarted.vault.diagnostics._connect() as connection:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM tombstones WHERE id = ?",
                            ("memory_legacy_task_scope",),
                        ).fetchone()
                    )
            finally:
                restarted.close()

    def test_invalid_inputs_have_domain_errors(self):
        for action, parameters in [
            ("memory_write", {"project_id": self.project, "title": "", "body": "x", "operation_id": "x"}),
            ("memory_search", {"project_id": self.project, "scope": "all"}),
            ("task_open", {"project_id": self.project, "title": "x", "goal": "x", "acceptance": "invalid", "operation_id": "x"}),
            ("unknown", {}),
        ]:
            with self.subTest(action=action), self.assertRaises(DiagnosticError):
                self.service.dispatch(action, parameters)

    def test_bounded_context_keeps_latest_next_step_before_history(self):
        task = self.call("task_open", project_id=self.project, title="长任务", goal="目标" * 3000,
                         acceptance=["验收"], operation_id="long")
        latest = self.call("task_read", task_id=task["id"])
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=latest["etag"],
                  findings="信息", progress="进行中", next_step="先验证刷新逻辑", operation_id="cp")
        context = self.call("context_get", project_id=self.project, task_id=task["id"], budget=500)
        self.assertIn("先验证刷新逻辑", context["text"])

    def test_chinese_single_character_search_and_invalid_forget_version(self):
        memory = self.call("memory_write", project_id=self.project, title="认证约束", body="规则",
                           sources=[{"type": "user", "reference": "输入"}], operation_id="single")
        self.assertEqual(len(self.call("memory_search", project_id=self.project, query="认")["items"]), 1)
        read = self.call("memory_read", memory_id=memory["id"])
        for revision in (True, 1.0):
            with self.assertRaises(DiagnosticError) as caught:
                self.call("memory_forget", memory_id=memory["id"], expected_revision=revision,
                          expected_etag=read["etag"], operation_id="invalid")
            self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def test_long_chinese_query_rejects_single_character_overlap(self):
        self.call("memory_write", project_id=self.project, title="任务接续方法",
                  body="切换工具前保存检查点，恢复时读取当前任务和下一步。",
                  sources=[{"type": "user", "reference": "输入"}], operation_id="continuation")
        self.call("memory_write", project_id=self.project, title="验收候选知识",
                  body="候选知识经核对并明确启用后，才进入常规检索与上下文。",
                  sources=[{"type": "host", "reference": "验收"}], operation_id="acceptance")
        hits = self.call("memory_search", project_id=self.project,
                         query="怎样在新窗口知道旧线程做到哪里")["items"]
        self.assertEqual(hits, [])
        exact = self.call("memory_search", project_id=self.project, query="任务接续")["items"]
        self.assertEqual([item["title"] for item in exact], ["任务接续方法"])

    def test_retrieval_cues_support_a_changed_language(self):
        memory = self.call(
            "memory_write",
            project_id=self.project,
            title="任务接续方法",
            body=(
                "切换工具前保存检查点，恢复时读取当前任务和下一步。\n"
                "Retrieval cues: resume previous work, another client, where left off"
            ),
            sources=[{"type": "user", "reference": "输入"}],
            operation_id="cued-continuation",
        )
        hits = self.call("memory_search", project_id=self.project,
                         query="resume previous work in another client")["items"]
        self.assertEqual([item["id"] for item in hits], [memory["id"]])

    def test_checkpoint_can_record_progress_without_new_findings(self):
        task = self.call("task_open", project_id=self.project, title="任务", goal="目标",
                         acceptance=["检查"], operation_id="t")
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1,
                  expected_etag=task["etag"], findings="", progress="代码已更新",
                  next_step="运行检查", operation_id="cp")
        restored = self.call("task_read", task_id=task["id"])
        self.assertEqual(restored["findings"], "")
        self.assertEqual(restored["progress"], "代码已更新")

    def test_mixed_language_casefold_and_user_context_opt_in(self):
        self.call("memory_write", project_id=self.project, title="OAuth 刷新约束", body="Use Refresh Token",
                  sources=[{"type": "user", "reference": "输入"}], operation_id="mixed")
        self.call("memory_write", scope="user", title="个人输出偏好", body="偏好短段落与中文说明",
                  sources=[{"type": "user", "reference": "明确偏好"}], operation_id="preference")
        english = self.call("memory_search", project_id=self.project, query="REFRESH TOKEN")["items"]
        self.assertEqual([item["title"] for item in english], ["OAuth 刷新约束"])
        normal = self.call("context_get", project_id=self.project)["text"]
        explicit = self.call("context_get", project_id=self.project, include_user=True)["text"]
        self.assertNotIn("偏好短段落", normal)
        self.assertIn("偏好短段落", explicit)

    def test_completed_task_keeps_consolidation_pending_until_explicit_review(self):
        task = self.call("task_open", project_id=self.project, title="认证经验", goal="修复认证",
                         acceptance=["测试通过"], operation_id="review-task")
        self.call("task_close", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                  outcome="completed", summary="测试通过", acceptance_results={"测试通过": "unittest 通过"},
                  operation_id="review-close")
        review = self.call("memory_review", task_id=task["id"])
        self.assertEqual(review["state"], "pending")
        self.assertEqual(review["task"]["status"], "completed")
        memory = self.call("memory_write", project_id=self.project, title="认证经验", body="验证令牌过期",
                           sources=[{"type": "task", "reference": task["id"]}],
                           status="candidate", operation_id="candidate")
        self.assertEqual(self.call("memory_search", project_id=self.project, query="认证")["items"], [])
        self.assertEqual(len(self.call("memory_search", project_id=self.project, query="认证",
                                       status="candidate")["items"]), 1)
        activated = self.call("memory_activate", memory_id=memory["id"], expected_revision=1,
                              expected_etag=memory["etag"], operation_id="activate")
        latest = review["task"]
        self.call("memory_review_complete", task_id=task["id"], expected_revision=latest["revision"],
                  expected_etag=latest["etag"], memory_ids=[activated["id"]], summary="已去重并激活一条经验",
                  operation_id="review-complete")
        with Service(self.service.vault.home) as restarted:
            reviewed = restarted.dispatch("memory_review", {"task_id": task["id"]})["data"]
            self.assertEqual(reviewed["state"], "reviewed")
            self.assertEqual(reviewed["task"]["consolidation_memory_ids"], [memory["id"]])
        self.assertEqual(len(self.call("memory_search", project_id=self.project, query="认证")["items"]), 1)

    def test_completed_task_review_ignores_residual_lock_without_live_claim(self):
        task = self.call(
            "task_open", project_id=self.project, title="关闭后审查", goal="验证残留锁",
            acceptance=["审查可完成"], operation_id="residual-review-task",
        )
        closed = self.call(
            "task_close", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], outcome="completed", summary="任务完成",
            acceptance_results={"审查可完成": "已关闭"}, operation_id="residual-review-close",
        )
        other = Service(self.service.vault.home)
        other.start()
        self.addCleanup(other.close)
        _, state = other.execution.claim(task["id"], self.project)
        self.assertEqual(state, "acquired")
        _, claim_path = other.execution._paths(task["id"])
        claim_path.unlink()
        self.assertIsNone(other.execution.current(task["id"]))

        self.call(
            "memory_review_complete", task_id=task["id"],
            expected_revision=closed["revision"], expected_etag=closed["etag"],
            memory_ids=[], summary="没有可复用知识，关闭后审查仍可完成",
            operation_id="residual-review-complete",
        )
        reviewed = self.call("task_read", task_id=task["id"])
        self.assertEqual(reviewed["consolidation_status"], "reviewed")

    def test_task_review_requires_completed_status_and_no_live_foreign_claim(self):
        current = self.call(
            "task_open", project_id=self.project, title="仍进行中", goal="拒绝提前审查",
            acceptance=["不可提前归档"], operation_id="current-review-task",
        )
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "memory_review_complete", task_id=current["id"],
                expected_revision=current["revision"], expected_etag=current["etag"],
                memory_ids=[], summary="不应写入", operation_id="current-review-complete",
            )
        self.assertEqual(caught.exception.code, "TASK_NOT_COMPLETED")

        closed = self.call(
            "task_close", task_id=current["id"], expected_revision=current["revision"],
            expected_etag=current["etag"], outcome="completed", summary="先关闭",
            acceptance_results={"不可提前归档": "已关闭"},
            operation_id="current-review-close",
        )
        foreign = Service(self.service.vault.home)
        foreign.start()
        self.addCleanup(foreign.close)
        claim, state = foreign.execution.claim(current["id"], self.project)
        self.assertEqual(state, "acquired")
        self.assertTrue(claim["claim_id"])
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "memory_review_complete", task_id=current["id"],
                expected_revision=closed["revision"], expected_etag=closed["etag"],
                memory_ids=[], summary="活 claim 不应穿透",
                operation_id="foreign-review-complete",
            )
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")

    def test_phase_review_is_blocked_by_a_live_foreign_claim(self):
        task = self.call(
            "task_open", project_id=self.project, title="阶段审查", goal="验证租约",
            acceptance=["阶段审查受租约约束"], operation_id="phase-review-task",
        )
        phase = self.call(
            "task_phase_close", task_id=task["id"],
            expected_revision=task["revision"], expected_etag=task["etag"],
            title="检查", summary="阶段完成", next_step="审查",
            operation_id="phase-review-close",
        )
        foreign = Service(self.service.vault.home)
        foreign.start()
        self.addCleanup(foreign.close)
        _, state = foreign.execution.claim(task["id"], self.project)
        self.assertEqual(state, "acquired")
        review = self.call("memory_review", task_id=task["id"], phase_id=phase["phase_id"])
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "memory_review_complete", task_id=task["id"], phase_id=phase["phase_id"],
                expected_revision=review["phase"]["revision"],
                expected_etag=review["phase"]["etag"], memory_ids=[],
                summary="外部活 claim 不应穿透", operation_id="foreign-phase-review",
            )
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")

    def test_compaction_snapshot_is_claim_exempt_but_keeps_cas_and_ownership(self):
        task = self.call(
            "task_open", project_id=self.project, title="压缩快照", goal="保留压缩证据",
            acceptance=["快照写入任务"], operation_id="compaction-task",
        )
        holder = Service(self.service.vault.home)
        holder.start()
        self.addCleanup(holder.close)
        claim, state = holder.execution.claim(task["id"], self.project)
        self.assertEqual(state, "acquired")
        self.assertFalse(self.service.execution.is_authorized(task["id"]))

        # The ordinary driver checkpoint path still requires execution ownership.
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
                expected_etag=task["etag"], progress="blocked", next_step="blocked",
                operation_id="blocked-checkpoint",
            )
        self.assertEqual(caught.exception.code, "EXECUTION_CLAIM_REQUIRED")

        # The PreCompact snapshot path is a narrow recovery-evidence exception.
        snapshot = dict(
            task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"],
            findings="[Pre-compaction snapshot]\nCaptured before compaction.",
            progress="Pre-compaction snapshot recorded.", next_step="Continue after compaction",
            operation_id="compaction-snapshot",
        )
        response = self.service.task_compaction_snapshot(**snapshot)
        self.assertEqual(response["revision"], task["revision"] + 1)
        self.assertEqual(response["origin"], "precompact_hook")
        latest = self.call("task_read", task_id=task["id"])
        self.assertEqual(latest["revision"], task["revision"] + 1)
        self.assertIn("[Pre-compaction snapshot]", latest["findings"])
        self.assertEqual(latest["next_step"], "Continue after compaction")
        self.assertEqual(holder.execution.current(task["id"])["claim_id"], claim["claim_id"])
        self.assertTrue(holder.execution.is_authorized(task["id"]))
        self.assertFalse(self.service.execution.is_authorized(task["id"]))

        # Same operation_id replays idempotently instead of bumping again.
        replay = self.service.task_compaction_snapshot(**snapshot)
        self.assertEqual(replay["revision"], latest["revision"])
        self.assertEqual(self.call("task_read", task_id=task["id"])["revision"], latest["revision"])

        # Revision/etag CAS is retained for the snapshot path too.
        with self.assertRaises(DiagnosticError) as caught:
            self.service.task_compaction_snapshot(
                task["id"], task["revision"], task["etag"],
                findings="stale snapshot", progress="stale", next_step="stale",
                operation_id="compaction-stale",
            )
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")

        # It is not part of the MCP/CLI action surface.
        with self.assertRaises(DiagnosticError) as caught:
            self.service.dispatch("task_compaction_snapshot", dict(
                task_id=task["id"], expected_revision=task["revision"],
                expected_etag=task["etag"], findings="x", progress="p", next_step="n",
                operation_id="dispatch-snapshot",
            ))
        self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def test_workspace_prepare_does_not_register_or_choose_a_task_for_new_work(self):
        task = self.call("task_open", project_id=self.project, title="已有任务", goal="原目标",
                         acceptance=["原验收"], operation_id="existing-task")
        before = self.call("status")["counts"]
        prepared = self.call("workspace_prepare", root=str(self.root), intent="new")
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(prepared["tasks"], [])
        self.assertEqual(prepared["background_task"]["id"], task["id"])
        self.assertIsNone(prepared["execution"])
        self.assertNotIn(task["id"], prepared["context"]["candidate_sources"])
        self.assertNotIn("原目标", prepared["context"]["text"])
        unregistered = self.root.parent / "unregistered"
        unregistered.mkdir()
        missing = self.call("workspace_prepare", root=str(unregistered), intent="resume")
        self.assertEqual(missing["state"], "registration_required")
        self.assertEqual(self.call("status")["counts"], before)
        for invalid in (42, [], ""):
            with self.subTest(task_id=invalid), self.assertRaises(DiagnosticError):
                self.call("workspace_prepare", root=str(unregistered), intent="resume", task_id=invalid)

    def test_project_members_are_derived_and_not_injected_into_children(self):
        b_root = self.root / "apps" / "b"
        c_root = self.root / "apps" / "c"
        d_root = b_root / "libs" / "d"
        for root in (b_root, c_root, d_root):
            root.mkdir(parents=True)
        b = self.call("project_register", root=str(b_root), name="B", operation_id="pb")["id"]
        c = self.call("project_register", root=str(c_root), name="C", operation_id="pc")["id"]
        d = self.call("project_register", root=str(d_root), name="D", operation_id="pd")["id"]

        listed = {item["id"]: item for item in self.call("project_list")["items"]}
        self.assertIsNone(listed[self.project]["parent_id"])
        self.assertEqual(listed[b]["parent_id"], self.project)
        self.assertEqual(listed[c]["parent_id"], self.project)
        self.assertEqual(listed[d]["parent_id"], b)

        task_a = self.call("task_open", project_id=self.project, title="A 任务", goal="A",
                           acceptance=["A"], operation_id="ta")
        task_b = self.call("task_open", project_id=b, title="B 任务", goal="B",
                           acceptance=["B"], operation_id="tb")
        task_c = self.call("task_open", project_id=c, title="C 任务", goal="C",
                           acceptance=["C"], operation_id="tc")
        task_d = self.call("task_open", project_id=d, title="D 任务", goal="D",
                           acceptance=["D"], operation_id="td")

        before_b = self.call("task_read", task_id=task_b["id"])
        prepared_a = self.call("workspace_prepare", root=str(self.root), intent="inspect")
        after_b = self.call("task_read", task_id=task_b["id"])
        self.assertEqual(
            (before_b["revision"], before_b["etag"]),
            (after_b["revision"], after_b["etag"]),
        )
        self.assertEqual([item["id"] for item in prepared_a["tasks"]], [task_a["id"]])
        members_a = {item["id"]: item for item in prepared_a["members"]}
        self.assertEqual(set(members_a), {b, c})
        self.assertNotIn(d, members_a)
        self.assertEqual(members_a[b]["current"]["id"], task_b["id"])
        self.assertEqual(members_a[b]["task_counts"]["current"], 1)
        self.assertEqual(members_a[c]["current"]["id"], task_c["id"])
        self.assertFalse(prepared_a["members_truncated"])

        prepared_b = self.call("workspace_prepare", root=str(b_root), intent="inspect")
        self.assertEqual(prepared_b["project"]["id"], b)
        self.assertEqual([item["id"] for item in prepared_b["tasks"]], [task_b["id"]])
        self.assertEqual([item["id"] for item in prepared_b["members"]], [d])
        self.assertEqual(prepared_b["members"][0]["current"]["id"], task_d["id"])
        self.assertNotIn(task_a["id"], [item["id"] for item in prepared_b["tasks"]])
        self.assertNotIn(self.project, [item["id"] for item in prepared_b["members"]])

    def test_group_knowledge_is_opt_in_for_children_and_members(self):
        b_root = self.root / "services" / "b"
        c_root = self.root / "services" / "c"
        for root in (b_root, c_root):
            root.mkdir(parents=True)
        b = self.call("project_register", root=str(b_root), name="B", operation_id="p2-b")["id"]
        c = self.call("project_register", root=str(c_root), name="C", operation_id="p2-c")["id"]
        source = [{"type": "user", "reference": "P17 组知识测试"}]
        group = self.call(
            "memory_write", project_id=self.project, scope="group", title="共享组知识",
            body="quasar shared conclusion", sources=source, status="active",
            operation_id="p2-group",
        )
        member_b = self.call(
            "memory_write", project_id=b, scope="project", title="B 成员知识",
            body="bravo member note", sources=source, status="active", operation_id="p2-member-b",
        )
        member_c = self.call(
            "memory_write", project_id=c, scope="project", title="C 成员知识",
            body="charlie member note", sources=source, status="active", operation_id="p2-member-c",
        )
        project_a = self.call(
            "memory_write", project_id=self.project, scope="project", title="A 项目知识",
            body="delta project note", sources=source, status="active", operation_id="p2-project-a",
        )

        self.assertEqual(
            self.call("memory_search", project_id=b, query="quasar")["items"], []
        )
        group_hits = self.call(
            "memory_search", project_id=b, query="quasar", include_group=True
        )["items"]
        self.assertEqual([item["id"] for item in group_hits], [group["id"]])
        self.assertEqual(
            self.call("memory_search", project_id=self.project, query="bravo")["items"], []
        )
        member_hits = self.call(
            "memory_search", project_id=self.project, query="bravo", include_members=True
        )["items"]
        self.assertEqual([item["id"] for item in member_hits], [member_b["id"]])

        self.assertNotIn("共享组知识", self.call(
            "context_get", project_id=b, query="quasar"
        )["text"])
        self.assertIn("共享组知识", self.call(
            "context_get", project_id=b, query="quasar", include_group_knowledge=True
        )["text"])
        default_prepare = self.call(
            "workspace_prepare", root=str(b_root), intent="new", query="quasar"
        )
        self.assertNotIn("共享组知识", default_prepare["context"]["text"])
        group_prepare = self.call(
            "workspace_prepare", root=str(b_root), intent="new",
            query="quasar", include_group_knowledge=True,
        )
        self.assertIn("共享组知识", group_prepare["context"]["text"])

        prepared_a = self.call("workspace_prepare", root=str(self.root), intent="inspect")
        members_a = {item["id"]: item for item in prepared_a["members"]}
        self.assertEqual(members_a[b]["knowledge_counts"]["active"], 1)
        self.assertEqual(members_a[b]["recent_knowledge"][0]["id"], member_b["id"])
        self.assertEqual(self.call("status")["knowledge"]["by_scope"]["group"], 1)
        self.assertEqual(
            self.call("memory_read", memory_id=project_a["id"])["scope"], "project"
        )
        self.assertEqual(
            self.call("memory_read", memory_id=member_c["id"])["scope"], "project"
        )

        with self.assertRaises(DiagnosticError) as leaf_group:
            self.call(
                "memory_write", project_id=b, scope="group", title="leaf group",
                body="not shared", sources=source, status="active", operation_id="p2-leaf-group",
            )
        self.assertEqual(leaf_group.exception.code, "SCOPE_DENIED")

    def test_workspace_prepare_resumes_current_and_does_not_resume_queued(self):
        tasks = [
            self.call("task_open", project_id=self.project, title=title, goal="目标",
                      acceptance=["检查"], operation_id=title)
            for title in ("任务甲", "任务乙")
        ]
        self.assertEqual(tasks[0]["status"], "current")
        self.assertEqual(tasks[1]["status"], "queued")
        resumed = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(resumed["state"], "ready")
        self.assertEqual(resumed["task_id"], tasks[0]["id"])
        self.assertEqual([item["id"] for item in resumed["tasks"]],
                         [tasks[0]["id"], tasks[1]["id"]])
        self.assertIn(tasks[0]["id"], resumed["context"]["candidate_sources"])
        self.assertNotIn(tasks[1]["id"], resumed["context"]["text"])
        with self.assertRaises(DiagnosticError) as caught:
            self.call("workspace_prepare", root=str(self.root), intent="resume",
                      task_id=tasks[1]["id"])
        self.assertEqual(caught.exception.code, "TASK_NOT_CURRENT")
        inspect = self.call("workspace_prepare", root=str(self.root))
        self.assertIsNone(inspect["context"])

    def test_workspace_prepare_resumes_unique_task_after_restart_and_rejects_other_scope(self):
        task = self.call("task_open", project_id=self.project, title="恢复任务", goal="保留目标",
                         acceptance=["检查"], operation_id="resume-task")
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                  findings="定位完成", progress="实现中", next_step="验证会话过期", operation_id="resume-cp")
        with Service(self.service.vault.home) as restarted:
            restored = restarted.dispatch("workspace_prepare", {"root": str(self.root), "intent": "resume"})["data"]
            self.assertEqual(restored["state"], "ready")
            self.assertIn("验证会话过期", restored["context"]["text"])
        other = self.root.parent / "other"
        other.mkdir()
        self.call("project_register", root=str(other), name="其他", operation_id="other")
        with self.assertRaises(DiagnosticError) as caught:
            self.call("workspace_prepare", root=str(other), intent="resume", task_id=task["id"])
        self.assertEqual(caught.exception.code, "SCOPE_DENIED")
        for parameters in ({"root": "."}, {"root": str(self.root), "intent": "guess"},
                           {"root": str(self.root), "intent": "new", "task_id": task["id"]}):
            with self.subTest(parameters=parameters), self.assertRaises(DiagnosticError):
                self.service.dispatch("workspace_prepare", parameters)

    def test_workspace_prepare_lists_pending_consolidation_without_resuming_closed_task(self):
        task = self.call("task_open", project_id=self.project, title="待提炼", goal="目标",
                         acceptance=["验证"], operation_id="pending-task")
        self.call("task_close", task_id=task["id"], expected_revision=task["revision"],
                  expected_etag=task["etag"], outcome="completed", summary="验证完成",
                  acceptance_results={"验证": "实际运行通过"}, operation_id="close-pending")
        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["state"], "no_active_task")
        self.assertIsNone(prepared["context"])
        self.assertEqual(prepared["pending_consolidation"][0]["id"], task["id"])
        self.assertIn(
            "pending_consolidation",
            {item["code"] for item in prepared["attention"]},
        )

    def test_workspace_prepare_exposes_reboot_attention_and_completion(self):
        closed = self.call(
            "task_open", project_id=self.project, title="closed task", goal="close",
            acceptance=["closed"], operation_id="closed-task",
        )
        self.call(
            "task_close", task_id=closed["id"], expected_revision=closed["revision"],
            expected_etag=closed["etag"], outcome="completed", summary="closed",
            acceptance_results={"closed": "verified"}, operation_id="close-closed-task",
        )
        current = self.call(
            "task_open", project_id=self.project, title="current task", goal="continue",
            acceptance=["current visible"], operation_id="current-task",
        )
        prepared = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(prepared["task_id"], current["id"])
        self.assertEqual(prepared["context"]["reboot"]["next"],
                         "No next step recorded; set one before continuing.")
        self.assertEqual(prepared["completion"]["state"], "ready")
        self.assertIn("pending_consolidation",
                      {item["code"] for item in prepared["context"]["attention"]})
        self.assertIn("pending_consolidation",
                      {item["code"] for item in prepared["attention"]})


    def test_workspace_prepare_accepts_optional_active_only_knowledge_index(self):
        task = self.call(
            "task_open", project_id=self.project, title="Index opt-in task",
            goal="Inject the active knowledge index only when requested",
            acceptance=["index visible"], operation_id="index-prepare-task",
        )
        memory = self.call(
            "memory_write", project_id=self.project, title="ACTIVE_PREPARE_INDEX",
            body="Navigation entry for workspace prepare.",
            sources=[{"type": "host", "reference": "seed"}], operation_id="index-prepare-memory",
        )
        candidate = self.call(
            "memory_write", project_id=self.project, title="CANDIDATE_PREPARE_INDEX",
            body="Candidate navigation entry stays out.",
            sources=[{"type": "host", "reference": "seed"}], status="candidate",
            operation_id="index-prepare-candidate",
        )
        default = self.call("workspace_prepare", root=str(self.root), intent="resume")
        explicit_off = self.call(
            "workspace_prepare", root=str(self.root), intent="resume",
            include_knowledge_index=False,
        )
        enabled = self.call(
            "workspace_prepare", root=str(self.root), intent="resume",
            include_knowledge_index=True,
        )
        self.assertEqual(default["state"], "ready")
        self.assertEqual(default["task_id"], task["id"])
        self.assertNotIn("knowledge_index", default["context"])
        self.assertEqual(
            json.dumps(default["context"], ensure_ascii=False, sort_keys=True),
            json.dumps(explicit_off["context"], ensure_ascii=False, sort_keys=True),
        )
        metadata = enabled["context"]["knowledge_index"]
        self.assertTrue(metadata["injected"])
        self.assertIn("Knowledge index (navigation only; not evidence)", enabled["context"]["text"])
        self.assertIn(memory["id"], enabled["context"]["text"])
        self.assertNotIn(candidate["id"], enabled["context"]["text"])
        self.assertLessEqual(len(enabled["context"]["text"]), 12000)
        for value in ("yes", 1, None):
            with self.subTest(value=value), self.assertRaises(DiagnosticError) as caught:
                self.call(
                    "workspace_prepare", root=str(self.root), intent="inspect",
                    include_knowledge_index=value,
                )
            self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def _sample_evidence(self):
        return [
            {
                "kind": "command",
                "reference": "python -m unittest tests.test_services",
                "hash": "abc123",
                "observed_at": "2026-11-08T10:00:00+08:00",
                "summary": "focused suite passed",
            },
            {"kind": "file", "reference": "src/memweave/storage.py", "hash": "sha256:deadbeef"},
            {"kind": "user", "reference": "用户要求证据可回放"},
        ]

    def test_checkpoint_evidence_persists_reads_back_and_merges(self):
        task = self.call(
            "task_open", project_id=self.project, title="证据任务", goal="保留证据",
            acceptance=["读回证据"], operation_id="evidence-task",
        )
        self.assertEqual(self.call("task_read", task_id=task["id"])["evidence"], [])
        evidence = self._sample_evidence()
        first = self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="定位完成", progress="实现中",
            next_step="验证证据", evidence=evidence, operation_id="evidence-cp-1",
        )
        detail = self.call("task_read", task_id=task["id"])
        self.assertEqual(detail["evidence"], evidence)
        self.assertEqual(detail["checkpoints"][0]["evidence"], evidence)
        checkpoint_file = self.service.vault.home / "checkpoints" / f"{first['checkpoint_id']}.md"
        markdown = checkpoint_file.read_text("utf-8")
        self.assertIn('"evidence"', markdown)
        self.assertIn("## Evidence", markdown)
        self.assertIn("python -m unittest tests.test_services", markdown)
        # Omitting evidence preserves the current pointers; an explicit [] clears them.
        second = self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=first["revision"],
            expected_etag=first["etag"], progress="继续实现", next_step="继续验证",
            operation_id="evidence-cp-2",
        )
        preserved = self.call("task_read", task_id=task["id"])
        self.assertEqual(preserved["checkpoints"][-1]["evidence"], evidence)
        self.assertEqual(preserved["evidence"], evidence)
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=second["revision"],
            expected_etag=second["etag"], progress="清除旧证据", next_step="重新收集",
            evidence=[], operation_id="evidence-cp-3",
        )
        latest = self.call("task_read", task_id=task["id"])
        self.assertEqual(latest["evidence"], [])
        self.assertEqual(latest["checkpoints"][-1]["evidence"], [])
        self.assertEqual(latest["checkpoints"][0]["evidence"], evidence)
        # The stored evidence survives a Markdown rebuild without duplication.
        self.call("rebuild")
        rebuilt = self.call("task_read", task_id=task["id"])
        self.assertEqual(rebuilt["checkpoints"][0]["evidence"], evidence)
        self.assertEqual(len(rebuilt["checkpoints"][0]["evidence"]), len(evidence))

    def test_checkpoint_and_memory_evidence_reject_invalid_inputs(self):
        task = self.call(
            "task_open", project_id=self.project, title="无效证据", goal="拒绝坏证据",
            acceptance=["拒绝"], operation_id="invalid-evidence-task",
        )
        too_many = [{"kind": "host", "reference": f"ptr-{index}"} for index in range(21)]
        invalid_values = [
            "not-a-list",
            [1],
            [{"kind": "git", "reference": "HEAD"}],
            [{"kind": "file"}],
            [{"kind": "file", "reference": "x" * 501}],
            too_many,
        ]
        for index, value in enumerate(invalid_values):
            with self.subTest(index=index), self.assertRaises(DiagnosticError) as caught:
                self.call(
                    "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
                    expected_etag=task["etag"], progress="x", next_step="y",
                    evidence=value, operation_id=f"invalid-evidence-{index}",
                )
            self.assertEqual(caught.exception.code, "RECORD_INVALID")
        self.assertEqual(self.call("task_read", task_id=task["id"])["revision"], task["revision"])
        with self.assertRaises(DiagnosticError) as caught:
            self.call(
                "memory_write", project_id=self.project, title="坏知识", body="不应写入",
                sources=[{"type": "host", "reference": "x"}],
                evidence=[{"kind": "git", "reference": "HEAD"}],
                operation_id="invalid-memory-evidence",
            )
        self.assertEqual(caught.exception.code, "RECORD_INVALID")
        self.assertEqual(
            self.call("memory_search", project_id=self.project, query="", status="candidate")["items"],
            [],
        )

    def test_memory_evidence_write_update_and_search(self):
        evidence = self._sample_evidence()
        written = self.call(
            "memory_write", project_id=self.project, title="证据知识", body="刷新令牌需记录证据",
            sources=[{"type": "user", "reference": "明确输入"}], evidence=evidence,
            operation_id="evidence-memory",
        )
        self.assertEqual(self.call("memory_read", memory_id=written["id"])["evidence"], evidence)
        hits = self.call("memory_search", project_id=self.project, query="证据知识")["items"]
        self.assertEqual(hits[0]["evidence"], evidence)
        self.call("rebuild")
        rebuilt_hits = self.call("memory_search", project_id=self.project, query="证据知识")["items"]
        self.assertEqual(rebuilt_hits[0]["evidence"], evidence)
        self.assertEqual(len(rebuilt_hits[0]["evidence"]), len(evidence))
        updated = self.call(
            "memory_update", memory_id=written["id"],
            expected_revision=written["revision"], expected_etag=written["etag"],
            title="证据知识", body="修订后正文", operation_id="evidence-memory-update",
        )
        self.assertEqual(self.call("memory_read", memory_id=written["id"])["evidence"], evidence)
        replacement = [{"kind": "test", "reference": "tests.test_services", "observed_at": "2026-11-08"}]
        second = self.call(
            "memory_update", memory_id=written["id"], expected_revision=updated["revision"],
            expected_etag=updated["etag"], title="证据知识", body="替换证据",
            evidence=replacement, operation_id="evidence-memory-replace",
        )
        self.assertEqual(self.call("memory_read", memory_id=written["id"])["evidence"], replacement)
        self.call(
            "memory_update", memory_id=written["id"], expected_revision=second["revision"],
            expected_etag=second["etag"], title="证据知识", body="清除证据",
            evidence=[], operation_id="evidence-memory-clear",
        )
        self.assertEqual(self.call("memory_read", memory_id=written["id"])["evidence"], [])
        self.assertEqual(
            self.call("memory_search", project_id=self.project, query="证据知识")["items"][0]["evidence"],
            [],
        )

    def test_legacy_records_without_evidence_read_as_empty(self):
        task = self.call(
            "task_open", project_id=self.project, title="旧记录", goal="兼容旧数据",
            acceptance=["读回空证据"], operation_id="legacy-evidence-task",
        )
        legacy_checkpoint = {
            "id": "checkpoint_legacy01", "type": "checkpoint", "scope": "task",
            "project_id": self.project, "task_id": task["id"], "title": "Checkpoint 1: 旧记录",
            "sequence": 1, "status": "recorded", "body": "# 旧检查点\n",
            "revision": 1, "created_at": "2026-11-07T00:00:00+00:00",
            "updated_at": "2026-11-07T00:00:00+00:00", "origin": "task_driver",
            "findings": "旧发现", "progress": "旧进展", "failures": "",
            "verification": "", "next_step": "旧下一步",
        }
        self.service.vault.commit("legacy-checkpoint", {"action": "legacy-checkpoint"}, lambda: (
            [{"record": legacy_checkpoint, "path": "checkpoints/checkpoint_legacy01.md", "expected": None}],
            {"id": "checkpoint_legacy01"},
        ))
        detail = self.call("task_read", task_id=task["id"])
        self.assertEqual(detail["checkpoints"][0]["id"], "checkpoint_legacy01")
        self.assertEqual(detail["checkpoints"][0]["evidence"], [])
        self.assertEqual(detail["evidence"], [])
        legacy_memory = {
            "id": "memory_legacy02", "type": "memory", "scope": "project",
            "project_id": self.project, "title": "旧知识", "body": "没有 evidence 字段",
            "revision": 1, "status": "active",
            "created_at": "2026-11-07T00:00:00+00:00", "updated_at": "2026-11-07T00:00:00+00:00",
            "kind": "fact", "sources": [{"type": "user", "reference": "旧输入"}],
        }
        self.service.vault.commit("legacy-memory", {"action": "legacy-memory"}, lambda: (
            [{"record": legacy_memory, "path": "knowledge/memory_legacy02.md", "expected": None}],
            {"id": "memory_legacy02"},
        ))
        self.service._refresh_all_index()
        self.assertEqual(self.call("memory_read", memory_id="memory_legacy02")["evidence"], [])
        hits = self.call("memory_search", project_id=self.project, query="旧知识")["items"]
        self.assertEqual([item["id"] for item in hits], ["memory_legacy02"])
        self.assertEqual(hits[0]["evidence"], [])

    def test_memory_review_returns_bounded_consolidation_package(self):
        task = self.call(
            "task_open", project_id=self.project, title="Auth refresh", goal="fix refresh",
            acceptance=["tests pass"], operation_id="consolidation-task",
        )
        checkpoint = self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="Found expired token path", progress="working",
            next_step="verify", evidence=self._sample_evidence(), operation_id="consolidation-cp",
        )
        detail = self.call("task_read", task_id=task["id"])
        phase = self.call(
            "task_phase_close", task_id=task["id"], expected_revision=detail["revision"],
            expected_etag=detail["etag"], title="Investigation", summary="root cause known",
            next_step="implement", operation_id="consolidation-phase",
        )
        closed = self.call("task_read", task_id=task["id"])
        self.call(
            "task_close", task_id=task["id"], expected_revision=closed["revision"],
            expected_etag=closed["etag"], outcome="completed", summary="done",
            acceptance_results={"tests pass": "unittest passed"}, operation_id="consolidation-close",
        )
        active = self.call(
            "memory_write", project_id=self.project, title="Token refresh fix",
            body="Refresh must record evidence pointers.", sources=[{"type": "task", "reference": task["id"]}],
            evidence=self._sample_evidence(), operation_id="consolidation-active",
        )
        duplicate = self.call(
            "memory_write", project_id=self.project, title="token   REFRESH fix",
            body="Duplicate title without evidence.", sources=[{"type": "host", "reference": "bulk"}],
            operation_id="consolidation-duplicate",
        )
        candidate = self.call(
            "memory_write", project_id=self.project, title="Candidate note", body="Needs review.",
            sources=[{"type": "host", "reference": "bulk"}], status="candidate",
            operation_id="consolidation-candidate",
        )
        retired_active = self.call(
            "memory_write", project_id=self.project, title="Old decision", body="No longer valid.",
            sources=[{"type": "host", "reference": "bulk"}], operation_id="consolidation-retired-write",
        )
        self.call(
            "memory_update", memory_id=retired_active["id"],
            expected_revision=retired_active["revision"], expected_etag=retired_active["etag"],
            title="Old decision", body="No longer valid.", status="retired",
            operation_id="consolidation-retired",
        )
        review = self.call("memory_review", task_id=task["id"])
        self.assertEqual(review["state"], "pending")
        self.assertIn("task", review)
        consolidation = review["consolidation"]
        self.assertEqual(consolidation["task_id"], task["id"])
        self.assertIsNone(consolidation["phase_id"])
        recent = consolidation["recent_checkpoints"]
        self.assertEqual(recent["total"], 1)
        self.assertEqual(len(recent["items"]), 1)
        self.assertEqual(recent["items"][0]["id"], checkpoint["checkpoint_id"])
        self.assertEqual(recent["items"][0]["findings"], "Found expired token path")
        self.assertEqual(recent["items"][0]["evidence"], self._sample_evidence())
        self.assertFalse(recent["truncated"])
        knowledge = consolidation["existing_knowledge"]
        self.assertEqual({item["id"] for item in knowledge["active"]}, {active["id"], duplicate["id"]})
        self.assertEqual({item["id"] for item in knowledge["candidate"]}, {candidate["id"]})
        self.assertEqual(knowledge["active_total"], 2)
        self.assertEqual(knowledge["candidate_total"], 1)
        self.assertLessEqual(len(knowledge["active"]), 50)
        active_summary = next(item for item in knowledge["active"] if item["id"] == active["id"])
        self.assertEqual(active_summary["evidence"], self._sample_evidence())
        self.assertEqual(active_summary["source_count"], 1)
        self.assertEqual(active_summary["status"], "active")
        lint = consolidation["lint"]
        self.assertEqual(lint["duplicate_titles"]["group_count"], 1)
        self.assertEqual(
            set(lint["duplicate_titles"]["groups"][0]["ids"]), {active["id"], duplicate["id"]}
        )
        without = lint["knowledge_without_evidence"]
        self.assertGreaterEqual(without["count"], 1)
        self.assertIn(duplicate["id"], without["ids"])
        self.assertNotIn(active["id"], without["ids"])
        self.assertEqual(lint["candidate_backlog"]["count"], 1)
        self.assertIsNotNone(lint["candidate_backlog"]["oldest_created_at"])
        self.assertEqual(lint["retired"]["count"], 1)
        self.assertIn("notes", lint)
        self.assertIsInstance(lint["truncated"], bool)
        self.assertIn("limits", consolidation)
        self.assertIn("recent_checkpoints", consolidation["limits"])
        self.assertIsInstance(consolidation["truncated"], bool)
        self.assertIn("pending_phases", consolidation)
        self.assertEqual(
            [item["id"] for item in consolidation["pending_phases"]["items"]], [phase["phase_id"]]
        )
        self.assertEqual(
            [item["id"] for item in consolidation["pending_consolidation"]["items"]], [task["id"]]
        )
        self.assertEqual(consolidation["pending_consolidation"]["items"][0]["lifecycle"], "pending_review")
        # Phase-scoped review still returns the phase envelope and its own package.
        phase_review = self.call(
            "memory_review", task_id=task["id"], phase_id=phase["phase_id"]
        )
        self.assertEqual(phase_review["phase"]["id"], phase["phase_id"])
        self.assertEqual(phase_review["consolidation"]["phase_id"], phase["phase_id"])

    def test_memory_review_consolidation_truncates_without_unbounded_output(self):
        task = self.call(
            "task_open", project_id=self.project, title="Bulk consolidation", goal="bound output",
            acceptance=["bounded"], operation_id="bulk-consolidation-task",
        )
        latest = task
        for index in range(12):
            latest = self.call(
                "task_checkpoint", task_id=task["id"], expected_revision=latest["revision"],
                expected_etag=latest["etag"], findings=f"finding {index}", progress=f"progress {index}",
                next_step="next", evidence=[{"kind": "test", "reference": f"tests.test_case_{index}"}],
                operation_id=f"bulk-checkpoint-{index}",
            )
        for index in range(51):
            self.call(
                "memory_write", project_id=self.project, title=f"bulk knowledge {index:02d}",
                body="bounded knowledge body", sources=[{"type": "host", "reference": "bulk"}],
                operation_id=f"bulk-memory-{index:02d}",
            )
        review = self.call("memory_review", task_id=task["id"])
        consolidation = review["consolidation"]
        recent = consolidation["recent_checkpoints"]
        self.assertEqual(recent["total"], 12)
        self.assertEqual(len(recent["items"]), 10)
        self.assertTrue(recent["truncated"])
        self.assertEqual([item["sequence"] for item in recent["items"]], list(range(12, 2, -1)))
        knowledge = consolidation["existing_knowledge"]
        self.assertEqual(knowledge["active_total"], 51)
        self.assertEqual(len(knowledge["active"]), 50)
        self.assertTrue(knowledge["active_truncated"])
        self.assertTrue(consolidation["truncated"])
        self.assertIn("active", consolidation["truncated_reasons"])
        self.assertTrue(consolidation["lint"]["knowledge_without_evidence"]["truncated"])
        self.assertLessEqual(len(consolidation["lint"]["knowledge_without_evidence"]["ids"]), 20)

    def _memory_record(self, record_id, status, title="record", body="body", evidence=None):
        stamp = "2026-11-09T00:00:00+00:00"
        record = {
            "id": record_id,
            "type": "memory",
            "scope": "project",
            "project_id": self.project,
            "title": title,
            "body": body,
            "revision": 1,
            "status": status,
            "kind": "fact",
            "sources": [{"type": "host", "reference": "seed"}],
            "created_at": stamp,
            "updated_at": stamp,
        }
        if evidence is not None:
            record["evidence"] = evidence
        return record

    def _seed_memory_records(self, records):
        plan = [
            {"record": record, "path": f"knowledge/{record['id']}.md", "expected": None}
            for record in records
        ]
        self.service.vault.commit(
            "seed-" + records[0]["id"], {"action": "seed", "count": len(records)},
            lambda: (plan, {"count": len(plan)}),
        )

    def test_knowledge_index_derives_one_line_active_candidate_and_retired(self):
        cued = self.call(
            "memory_write", project_id=self.project, title="Token refresh constraint",
            body="Apply only when refresh tokens are opaque.",
            sources=[{"type": "user", "reference": "明确输入"}],
            evidence=self._sample_evidence(), operation_id="index-active",
        )
        cues_only = self.call(
            "memory_write", project_id=self.project, title="Cue-only knowledge",
            body="## 适用范围\n\nRetrieval cues: 刷新令牌 refresh token",
            sources=[{"type": "host", "reference": "seed"}], operation_id="index-cues",
        )
        plain = self.call(
            "memory_write", project_id=self.project, title="Plain fact",
            body="First line is the summary.\nSecond line is only detail.",
            sources=[{"type": "host", "reference": "seed"}], operation_id="index-plain",
        )
        candidate = self.call(
            "memory_write", project_id=self.project, title="Unreviewed note",
            body="Candidate body.", sources=[{"type": "host", "reference": "seed"}],
            status="candidate", operation_id="index-candidate",
        )
        retired = self.call(
            "memory_write", project_id=self.project, title="Old rule", body="Retired body.",
            sources=[{"type": "host", "reference": "seed"}], operation_id="index-retired-write",
        )
        self.call(
            "memory_update", memory_id=retired["id"], expected_revision=retired["revision"],
            expected_etag=retired["etag"], title="Old rule", body="Superseded.",
            status="retired", operation_id="index-retired",
        )
        index = self.call("knowledge_index", project_id=self.project)
        self.assertEqual(index["format"], "one_line_per_record")
        self.assertEqual(index["project_id"], self.project)
        self.assertEqual(
            index["fields"],
            ["id", "kind", "status", "scope", "title", "summary", "evidence_count", "updated_at"],
        )
        self.assertEqual(index["reserved_fields"], ["links"])
        active = index["active"]
        self.assertEqual(active["total"], 3)
        self.assertEqual(active["returned"], 3)
        self.assertEqual(active["limit"], 120)
        self.assertEqual(active["char_limit"], 25000)
        self.assertFalse(active["truncated"])
        by_id = {}
        for line in active["items"]:
            self.assertIsInstance(line, str)
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), 240)
            fields = line.split(" | ")
            self.assertEqual(len(fields), 8)
            by_id[fields[0]] = fields
        cued_fields = by_id[cued["id"]]
        self.assertEqual(cued_fields[1], "fact")
        self.assertEqual(cued_fields[2], "active")
        self.assertEqual(cued_fields[3], "project")
        self.assertEqual(cued_fields[4], "Token refresh constraint")
        self.assertEqual(cued_fields[5], "Apply only when refresh tokens are opaque.")
        self.assertEqual(cued_fields[6], "evidence=3")
        self.assertTrue(cued_fields[7].startswith("updated="))
        self.assertEqual(by_id[cues_only["id"]][5], "刷新令牌 refresh token")
        self.assertEqual(by_id[plain["id"]][5], "First line is the summary.")
        self.assertEqual(by_id[plain["id"]][6], "evidence=0")
        candidate_section = index["candidate"]
        self.assertEqual(candidate_section["total"], 1)
        self.assertEqual(candidate_section["returned"], 1)
        self.assertEqual(candidate_section["limit"], 60)
        self.assertEqual(candidate_section["char_limit"], 12000)
        self.assertEqual(len(candidate_section["items"]), 1)
        self.assertTrue(candidate_section["items"][0].startswith(candidate["id"] + " | "))
        self.assertIn("| candidate | project |", candidate_section["items"][0])
        retired_section = index["retired"]
        self.assertEqual(retired_section["total"], 1)
        self.assertEqual(retired_section["ids"], [retired["id"]])
        self.assertNotIn("items", retired_section)
        self.assertFalse(retired_section["truncated"])
        self.assertFalse(index["truncated"])
        self.assertEqual(index["truncation_reasons"], [])

    def test_knowledge_index_enforces_row_limits_and_retired_id_bound(self):
        active = [
            self._memory_record(
                f"memory_a{position:031d}", "active",
                title=f"active {position:03d}", body=f"summary {position:03d}",
            )
            for position in range(125)
        ]
        candidate = [
            self._memory_record(
                f"memory_c{position:031d}", "candidate",
                title=f"candidate {position:03d}", body=f"pending {position:03d}",
            )
            for position in range(65)
        ]
        retired = [
            self._memory_record(
                f"memory_r{position:031d}", "retired", title=f"retired {position:03d}",
            )
            for position in range(5)
        ]
        self._seed_memory_records(active + candidate + retired)
        index = self.call("knowledge_index", project_id=self.project)
        active_section = index["active"]
        self.assertEqual(active_section["total"], 125)
        self.assertEqual(active_section["returned"], 120)
        self.assertEqual(len(active_section["items"]), 120)
        self.assertTrue(active_section["truncated"])
        self.assertIn("rows", active_section["truncation_reasons"])
        self.assertLessEqual(active_section["chars"], 25000)
        candidate_section = index["candidate"]
        self.assertEqual(candidate_section["total"], 65)
        self.assertEqual(candidate_section["returned"], 60)
        self.assertEqual(len(candidate_section["items"]), 60)
        self.assertTrue(candidate_section["truncated"])
        self.assertIn("rows", candidate_section["truncation_reasons"])
        self.assertLessEqual(candidate_section["chars"], 12000)
        retired_section = index["retired"]
        self.assertEqual(retired_section["total"], 5)
        self.assertEqual(len(retired_section["ids"]), 5)
        self.assertEqual(retired_section["limit"], 20)
        self.assertFalse(retired_section["truncated"])
        self.assertTrue(index["truncated"])
        self.assertIn("active_rows", index["truncation_reasons"])
        self.assertIn("candidate_rows", index["truncation_reasons"])
        for line in active_section["items"] + candidate_section["items"]:
            self.assertLessEqual(len(line), 240)

    def test_knowledge_index_enforces_char_budgets_and_clamps_long_lines(self):
        long_title = "T" * 250
        long_summary = "S" * 300
        active = [
            self._memory_record(
                f"memory_l{position:031d}", "active", title=long_title, body=long_summary,
            )
            for position in range(120)
        ]
        candidate = [
            self._memory_record(
                f"memory_m{position:031d}", "candidate", title=long_title, body=long_summary,
            )
            for position in range(60)
        ]
        self._seed_memory_records(active + candidate)
        index = self.call("knowledge_index", project_id=self.project)
        active_section = index["active"]
        self.assertEqual(active_section["total"], 120)
        self.assertLess(active_section["returned"], 120)
        self.assertGreater(active_section["chars"], 25000 - 240)
        self.assertLessEqual(active_section["chars"], 25000)
        self.assertIn("chars", active_section["truncation_reasons"])
        candidate_section = index["candidate"]
        self.assertLess(candidate_section["returned"], 60)
        self.assertGreater(candidate_section["chars"], 12000 - 240)
        self.assertLessEqual(candidate_section["chars"], 12000)
        self.assertIn("chars", candidate_section["truncation_reasons"])
        for line in active_section["items"] + candidate_section["items"]:
            self.assertLessEqual(len(line), 240)

    def test_knowledge_index_keeps_eight_columns_for_pipes_and_long_ids(self):
        piped = self._memory_record(
            "memory_" + "p" * 92, "active", title="left | right | title",
            body="summary | with | pipes",
            evidence=[{"kind": "host", "reference": "seed"}],
        )
        long_id = self._memory_record(
            "memory_" + "x" * 93, "active", title="T" * 200, body="S" * 300,
            evidence=[{"kind": "host", "reference": "seed"}],
        )
        self._seed_memory_records([piped, long_id])
        index = self.call("knowledge_index", project_id=self.project)
        for line in index["active"]["items"]:
            self.assertLessEqual(len(line), 240)
            fields = line.split(" | ")
            self.assertEqual(len(fields), 8)
            self.assertTrue(fields[7].startswith("updated="))
        by_id = {line.split(" | ")[0]: line.split(" | ") for line in index["active"]["items"]}
        self.assertEqual(by_id[piped["id"]][6], "evidence=1")
        self.assertNotIn("left | right", by_id[piped["id"]][4])
        self.assertEqual(by_id[long_id["id"]][6], "evidence=1")
        self.assertLessEqual(len(by_id[long_id["id"]][0]), 100)

    def test_knowledge_index_is_rebuilt_from_markdown_and_ignores_search_index(self):
        memory = self.call(
            "memory_write", project_id=self.project, title="Derived record",
            body="INDEX_BODY_BEFORE", sources=[{"type": "host", "reference": "seed"}],
            operation_id="index-derived",
        )
        knowledge_dir = self.service.vault.home / "knowledge"

        def knowledge_bytes():
            return {path.name: path.read_bytes() for path in sorted(knowledge_dir.glob("*.md"))}

        first = self.call("knowledge_index", project_id=self.project)
        before = knowledge_bytes()
        second = self.call("knowledge_index", project_id=self.project)
        self.assertEqual(first, second)
        self.assertEqual(knowledge_bytes(), before)
        path = knowledge_dir / f"{memory['id']}.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("INDEX_BODY_BEFORE", "INDEX_BODY_EDITED"),
            encoding="utf-8",
        )
        edited = self.call("knowledge_index", project_id=self.project)
        self.assertIn("INDEX_BODY_EDITED", edited["active"]["items"][0])
        self.assertNotEqual(edited, first)
        before_rebuild = knowledge_bytes()
        self.call("rebuild")
        rebuilt = self.call("knowledge_index", project_id=self.project)
        self.assertEqual(rebuilt, edited)
        self.assertEqual(knowledge_bytes(), before_rebuild)
        self.assertFalse(any(path.name.endswith((".db", "-wal", "-shm")) for path in knowledge_dir.iterdir()))

    def test_memory_review_embeds_the_same_knowledge_index(self):
        task = self.call(
            "task_open", project_id=self.project, title="Index integration", goal="embed index",
            acceptance=["index present"], operation_id="index-task",
        )
        detail = self.call("task_read", task_id=task["id"])
        phase = self.call(
            "task_phase_close", task_id=task["id"], expected_revision=detail["revision"],
            expected_etag=detail["etag"], title="Index phase", summary="phase done",
            next_step="finish", operation_id="index-phase",
        )
        closed = self.call("task_read", task_id=task["id"])
        self.call(
            "task_close", task_id=task["id"], expected_revision=closed["revision"],
            expected_etag=closed["etag"], outcome="completed", summary="done",
            acceptance_results={"index present": "unittest passed"}, operation_id="index-close",
        )
        written = self.call(
            "memory_write", project_id=self.project, title="Indexed knowledge",
            body="Navigation target.", sources=[{"type": "host", "reference": "seed"}],
            operation_id="index-knowledge",
        )
        standalone = self.call("knowledge_index", project_id=self.project)
        task_review = self.call("memory_review", task_id=task["id"])
        self.assertEqual(task_review["consolidation"]["knowledge_index"], standalone)
        self.assertIn("knowledge_index", task_review["consolidation"]["limits"])
        phase_review = self.call("memory_review", task_id=task["id"], phase_id=phase["phase_id"])
        self.assertEqual(phase_review["consolidation"]["knowledge_index"], standalone)
        self.assertIn(written["id"], task_review["consolidation"]["knowledge_index"]["active"]["items"][0])

    def test_knowledge_index_is_not_injected_into_context(self):
        self.call(
            "memory_write", project_id=self.project, title="Context target",
            body="CONTEXT_INJECTION_SENTINEL", sources=[{"type": "host", "reference": "seed"}],
            operation_id="context-index",
        )
        before = self.call("context_get", project_id=self.project)["text"]
        index = self.call("knowledge_index", project_id=self.project)
        self.assertEqual(index["active"]["total"], 1)
        after = self.call("context_get", project_id=self.project)["text"]
        self.assertEqual(before, after)
        prepared = self.call("workspace_prepare", root=str(self.root), intent="inspect")
        self.assertNotIn("knowledge_index", prepared)
        self.assertNotIn("one_line_per_record", before)


if __name__ == "__main__":
    unittest.main()
