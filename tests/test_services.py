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
        self.service.initialize()
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
        self.assertIn("令牌过期", restored["body"])
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
        reviewed = Service(self.service.vault.home).dispatch("memory_review", {"task_id": task["id"]})["data"]
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

    def test_workspace_prepare_requires_selection_when_resuming_multiple_tasks(self):
        tasks = [
            self.call("task_open", project_id=self.project, title=title, goal="目标",
                      acceptance=["检查"], operation_id=title)
            for title in ("任务甲", "任务乙")
        ]
        ambiguous = self.call("workspace_prepare", root=str(self.root), intent="resume")
        self.assertEqual(ambiguous["state"], "task_selection_required")
        self.assertEqual({item["id"] for item in ambiguous["tasks"]}, {item["id"] for item in tasks})
        self.assertIsNone(ambiguous["context"])
        explicit = self.call("workspace_prepare", root=str(self.root), intent="resume", task_id=tasks[0]["id"])
        self.assertEqual(explicit["state"], "ready")
        self.assertIn(tasks[0]["id"], explicit["context"]["candidate_sources"])
        self.assertNotIn(tasks[1]["id"], explicit["context"]["text"])
        inspect = self.call("workspace_prepare", root=str(self.root))
        self.assertIsNone(inspect["context"])

    def test_workspace_prepare_resumes_unique_task_after_restart_and_rejects_other_scope(self):
        task = self.call("task_open", project_id=self.project, title="恢复任务", goal="保留目标",
                         acceptance=["检查"], operation_id="resume-task")
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                  findings="定位完成", progress="实现中", next_step="验证会话过期", operation_id="resume-cp")
        restarted = Service(self.service.vault.home)
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


if __name__ == "__main__":
    unittest.main()
