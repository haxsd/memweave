import tempfile
import unittest
from pathlib import Path

from memweave.services import Service
from memweave.diagnostics import DiagnosticError


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.source = self.root / "auth.py"
        self.source.write_text("def refresh(): return 'token'", encoding="utf-8")
        self.service = Service(Path(self.temporary.name) / "home")
        self.service.initialize()
        self.project = self.call("project_register", root=str(self.root), name="Auth", operation_id="p")["id"]

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def map_architecture(self):
        self.call("project_scan", project_id=self.project, operation_id="scan")
        evidence = self.call("evidence_read", project_id=self.project, path="auth.py")
        return self.call("graph_write", project_id=self.project, operation_id="architecture",
                         title="auth refresh boundary", body="Refresh tokens are validated in auth.py.",
                         sources=[{"path": "auth.py", "hash": evidence["hash"]}],
                         relations=[{"source": "self", "target": "file:auth.py", "relation": "documented_by"}])

    def test_long_history_cannot_crowd_out_knowledge_and_current_project_structure(self):
        architecture = self.map_architecture()
        memory = self.call("memory_write", project_id=self.project, title="auth policy",
                           body="Keep refresh tokens outside browser scripts.",
                           sources=[{"type": "user", "reference": "explicit rule"}], operation_id="memory")
        task = self.call("task_open", project_id=self.project, title="auth fix", goal="Verify refresh expiry",
                         acceptance=["Expiry checked"], operation_id="task")
        self.call("task_checkpoint", task_id=task["id"], expected_revision=1, expected_etag=task["etag"],
                  findings="Old exploration. " * 600, progress="Handler located",
                  next_step="Run expiry regression", operation_id="checkpoint")
        context = self.call("context_get", project_id=self.project, task_id=task["id"], query="auth", budget=2400)
        self.assertIn("Run expiry regression", context["text"])
        self.assertIn("Keep refresh tokens", context["text"])
        self.assertIn("auth.py", context["text"])
        self.assertIn("auth refresh boundary", context["text"])
        self.assertLessEqual(len(context["text"]), 2400)
        self.assertTrue(context["truncated"])
        self.assertTrue({task["id"], memory["id"], architecture["id"], "file:auth.py"}.issubset(
            {source["id"] for source in context["included_sources"]}
        ))
        self.assertEqual(context["graph"]["freshness"]["state"], "checked")

    def test_context_excludes_changed_architecture_and_reports_scan_boundary(self):
        architecture = self.map_architecture()
        self.source.write_text("def refresh(): return 'changed'", encoding="utf-8")
        context = self.call("context_get", project_id=self.project, query="auth")
        self.assertNotIn(architecture["id"], context["text"])
        self.assertNotIn("Refresh tokens are validated", context["text"])
        self.assertEqual(context["graph"]["freshness"]["state"], "stale")
        self.assertEqual(context["graph"]["freshness"]["scope"], "indexed_sources_only")
        self.assertEqual(context["graph"]["freshness"]["stale_architecture_records"], 1)

    def test_user_preferences_are_opt_in_and_do_not_require_matching_the_code_query(self):
        preference = self.call("memory_write", scope="user", kind="preference", title="说明方式",
                               body="先说明结论，再列验证结果。", operation_id="preference",
                               sources=[{"type": "user", "reference": "长期明确偏好"}])
        candidate = self.call("memory_write", project_id=self.project, title="auth guess", body="UNVERIFIED",
                              status="candidate", operation_id="guess",
                              sources=[{"type": "host", "reference": "unconfirmed"}])
        normal = self.call("context_get", project_id=self.project, query="auth")
        explicit = self.call("context_get", project_id=self.project, query="auth", include_user=True)
        self.assertNotIn("先说明结论", normal["text"])
        self.assertIn("先说明结论", explicit["text"])
        self.assertNotIn(candidate["id"], explicit["candidate_sources"])
        self.assertIn(preference["id"], {source["id"] for source in explicit["included_sources"]})

    def test_emitted_source_ids_are_traceable_within_small_budgets(self):
        self.map_architecture()
        for position in range(5):
            self.call("memory_write", project_id=self.project, title=f"auth rule {position}",
                      body="Useful auth constraint. " * 200, operation_id=f"memory-{position}",
                      sources=[{"type": "user", "reference": "explicit"}])
        for budget in (500, 900, 12000):
            context = self.call("context_get", project_id=self.project, query="auth", budget=budget)
            with self.subTest(budget=budget):
                self.assertLessEqual(len(context["text"]), budget)
                for source in context["included_sources"]:
                    self.assertIn("[" + source["id"] + "]", context["text"])
                self.assertTrue(context["truncated"])

    def test_small_budget_keeps_a_useful_knowledge_excerpt_among_many_matches(self):
        for position in range(20):
            self.call("memory_write", project_id=self.project, title=f"auth rule {position}",
                      body="Keep refresh tokens outside browser scripts.", operation_id=f"rule-{position}",
                      sources=[{"type": "user", "reference": "explicit"}])
        context = self.call("context_get", project_id=self.project, query="auth", budget=900)
        self.assertIn("Keep refresh tokens", context["text"])
        self.assertTrue(context["included_sources"])
        self.assertTrue(context["truncated"])

    def test_architecture_is_prioritized_before_graph_result_limit(self):
        for position in range(45):
            (self.root / f"auth_{position}.py").write_text("pass", encoding="utf-8")
        architecture = self.map_architecture()
        context = self.call("context_get", project_id=self.project, query="auth")
        self.assertIn(architecture["id"], {source["id"] for source in context["included_sources"]})
        self.assertIn("host_reported", context["text"])

    def test_invalid_task_ids_cannot_silently_disable_task_scope(self):
        for invalid in ("", False, 0, []):
            with self.subTest(task_id=invalid), self.assertRaises(DiagnosticError):
                self.call("context_get", project_id=self.project, task_id=invalid)


if __name__ == "__main__":
    unittest.main()
