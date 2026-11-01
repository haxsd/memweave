import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.services import Service


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

    def test_user_scope_requires_explicit_user_source_and_no_task_binding(self):
        with self.assertRaises(DiagnosticError) as missing_user:
            self.call(
                "memory_write", scope="user", title="项目事实", body="不应成为跨项目记忆",
                sources=[{"type": "host", "reference": "自动观察"}], operation_id="user-host",
            )
        self.assertEqual(missing_user.exception.code, "SCOPE_DENIED")
        task = self.call(
            "task_open", project_id=self.project, title="任务", goal="目标",
            acceptance=["检查"], operation_id="user-task",
        )
        with self.assertRaises(DiagnosticError) as bound:
            self.call(
                "memory_write", scope="user", task_id=task["id"], title="绑定任务",
                body="不应绑定", sources=[{"type": "user", "reference": "明确输入"}],
                operation_id="user-bound",
            )
        self.assertEqual(bound.exception.code, "SCOPE_DENIED")

    def test_status_reports_knowledge_scope_and_architecture_freshness(self):
        task = self.call(
            "task_open", project_id=self.project, title="状态计数", goal="验证状态",
            acceptance=["计数正确"], operation_id="status-task",
        )
        self.call(
            "memory_write", project_id=self.project, scope="project", title="项目知识",
            body="项目范围", sources=[{"type": "user", "reference": "输入"}],
            operation_id="status-project-memory",
        )
        self.call(
            "memory_write", project_id=self.project, scope="task", task_id=task["id"],
            title="任务候选", body="任务范围", status="candidate",
            sources=[{"type": "task", "reference": task["id"]}],
            operation_id="status-task-memory",
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
        self.assertEqual(status["knowledge"]["total"], 3)
        self.assertEqual(status["knowledge"]["by_scope"], {"project": 1, "task": 1, "user": 1})
        self.assertEqual(status["knowledge"]["by_status"], {"active": 2, "candidate": 1})
        self.assertEqual(
            status["knowledge"]["by_scope_status"],
            {
                "project": {"active": 1, "candidate": 0},
                "task": {"active": 0, "candidate": 1},
                "user": {"active": 1, "candidate": 0},
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
        self.assertEqual(project_status["project"]["knowledge"]["total"], 2)
        self.assertEqual(
            project_status["project"]["knowledge"]["by_scope"],
            {"project": 1, "task": 1, "user": 0},
        )
        self.assertEqual(
            project_status["project"]["knowledge"]["by_status"],
            {"active": 1, "candidate": 1},
        )
        self.assertEqual(
            project_status["project"]["tasks"],
            {
                "total": 2, "current": 1, "queued": 1, "pending_review": 0,
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

    def test_task_scope_and_context_budget(self):
        task = self.call("task_open", project_id=self.project, title="任务", goal="目标",
                         acceptance=["检查"], operation_id="task")
        self.call("memory_write", project_id=self.project, scope="task", task_id=task["id"], title="局部",
                  body="任务专用知识", sources=[{"type": "user", "reference": "输入"}], operation_id="m")
        self.assertEqual(self.call("memory_search", project_id=self.project, scope="project", query="")["items"], [])
        context = self.call("context_get", project_id=self.project, task_id=task["id"], budget=2000)
        self.assertIn("任务专用知识", context["text"])
        self.assertLessEqual(len(context["text"]), 2000)
        with self.assertRaises(DiagnosticError):
            self.call("context_get", project_id="wrong", task_id=task["id"])

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

    def test_workspace_prepare_does_not_register_or_choose_a_task_for_new_work(self):
        task = self.call("task_open", project_id=self.project, title="已有任务", goal="原目标",
                         acceptance=["原验收"], operation_id="existing-task")
        before = self.call("status")["counts"]
        prepared = self.call("workspace_prepare", root=str(self.root), intent="new")
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(prepared["tasks"][0]["id"], task["id"])
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


if __name__ == "__main__":
    unittest.main()
