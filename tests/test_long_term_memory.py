import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import DiagnosticError
from memweave.services import Service


class LongTermMemoryTests(unittest.TestCase):
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
            "project_register", root=str(self.root), name="Long-term memory",
            operation_id="project",
        )["id"]

    def call(self, action, **parameters):
        return self.service.dispatch(action, parameters)["data"]

    def test_retired_decision_is_preserved_but_not_recalled_after_index_rebuild(self):
        memory = self.call(
            "memory_write", project_id=self.project, title="Experiment branch decision",
            body="Maintain an isolated quasar experiment branch.",
            sources=[{"type": "host", "reference": "Reviewed project decision"}],
            kind="decision", operation_id="branch-decision",
        )
        parameters = dict(
            memory_id=memory["id"], expected_revision=memory["revision"],
            expected_etag=memory["etag"], title="Experiment branch decision cancelled",
            body="The user cancelled the quasar experiment branch; continue on main.",
            status="retired", operation_id="cancel-branch-decision",
        )
        retired = self.call("memory_update", **parameters)
        self.assertEqual(self.call("memory_update", **parameters), retired)
        record = self.call("memory_read", memory_id=memory["id"])
        self.assertEqual(record["status"], "retired")
        self.assertTrue((self.home / record["path"]).is_file())
        with self.assertRaises(DiagnosticError) as conflict:
            self.call("memory_update", **{**parameters, "operation_id": "stale-correction"})
        self.assertEqual(conflict.exception.code, "VERSION_CONFLICT")
        self.service.rebuild()
        self.assertEqual(self.call(
            "memory_search", project_id=self.project, query="quasar",
        )["items"], [])
        context = self.call(
            "workspace_prepare", root=str(self.root), intent="new", query="quasar",
        )["context"]
        self.assertNotIn(memory["id"], context["candidate_sources"])
        history = self.call(
            "memory_search", project_id=self.project, query="quasar", status="retired",
        )
        self.assertEqual([item["id"] for item in history["items"]], [memory["id"]])
        self.assertEqual(self.call("status")["knowledge"]["by_status"]["retired"], 1)
        with self.assertRaises(DiagnosticError):
            self.call(
                "memory_activate", memory_id=memory["id"],
                expected_revision=retired["revision"], expected_etag=retired["etag"],
                operation_id="reactivate-retired-directly",
            )
        candidate = self.call(
            "memory_update", memory_id=memory["id"], expected_revision=retired["revision"],
            expected_etag=retired["etag"], title="Reconsidered branch decision",
            body="A newly reviewed quasar experiment may proceed.", status="candidate",
            operation_id="review-again",
        )
        self.call(
            "memory_activate", memory_id=memory["id"], expected_revision=candidate["revision"],
            expected_etag=candidate["etag"], operation_id="activate-reviewed-again",
        )
        self.assertEqual(self.call(
            "memory_search", project_id=self.project, query="quasar",
        )["matched"], 1)

    def test_memory_update_cannot_activate_candidate_or_accept_unknown_status(self):
        memory = self.call(
            "memory_write", project_id=self.project, title="Unreviewed decision",
            body="quasar assumption", status="candidate",
            sources=[{"type": "host", "reference": "Unverified hypothesis"}],
            operation_id="unreviewed",
        )
        for status in ("active", "deleted", ""):
            with self.subTest(status=status), self.assertRaises(DiagnosticError):
                self.call(
                    "memory_update", memory_id=memory["id"],
                    expected_revision=memory["revision"], expected_etag=memory["etag"],
                    title="Unreviewed decision", body="Still unverified.", status=status,
                    operation_id="invalid-status-" + status,
                )
        self.assertEqual(self.call("memory_read", memory_id=memory["id"])["revision"], 1)

    def test_phase_knowledge_candidate_correction_forget_and_next_task_reuse(self):
        first = self.call(
            "task_open", project_id=self.project, title="refresh boundary phase",
            goal="Preserve refresh boundary knowledge", acceptance=["phase review passes"],
            operation_id="first-task",
        )
        phase = self.call(
            "task_phase_close",
            task_id=first["id"],
            expected_revision=first["revision"],
            expected_etag=first["etag"],
            title="refresh investigation",
            summary="Refresh boundary is now understood",
            findings="Refresh needs an explicit boundary rule",
            verification="focused regression passed",
            next_step="Continue the task",
            operation_id="phase",
        )
        current = self.call("task_read", task_id=first["id"])
        self.assertEqual(current["status"], "current")
        review = self.call("memory_review", task_id=first["id"], phase_id=phase["phase_id"])
        self.assertEqual(review["state"], "pending")

        candidate = self.call(
            "memory_write",
            project_id=self.project,
            title="Refresh boundary knowledge",
            body="quasar refresh boundary sentinel keeps renewal explicit.",
            sources=[
                {"type": "task", "reference": first["id"]},
                {"type": "file", "reference": "src/auth.py"},
            ],
            status="candidate",
            kind="decision",
            operation_id="phase-knowledge",
        )
        hidden = self.call(
            "context_get", project_id=self.project, task_id=first["id"],
            query="quasar", budget=4000,
        )
        self.assertNotIn(candidate["id"], {item["id"] for item in hidden["included_sources"]})
        self.assertNotIn("quasar refresh boundary sentinel", hidden["text"])

        active = self.call(
            "memory_activate", memory_id=candidate["id"],
            expected_revision=candidate["revision"], expected_etag=candidate["etag"],
            operation_id="activate",
        )
        self.call(
            "memory_review_complete",
            task_id=first["id"],
            phase_id=phase["phase_id"],
            expected_revision=review["phase"]["revision"],
            expected_etag=review["phase"]["etag"],
            memory_ids=[candidate["id"]],
            summary="Phase evidence was reviewed and activated explicitly.",
            operation_id="phase-review",
        )
        phase_reuse = self.call(
            "context_get", project_id=self.project, task_id=first["id"],
            query="quasar", budget=4000,
        )
        self.assertIn(candidate["id"], {item["id"] for item in phase_reuse["included_sources"]})
        self.assertIn("quasar refresh boundary sentinel", phase_reuse["text"])
        self.assertEqual(self.call("task_read", task_id=first["id"])["status"], "current")

        unrelated = self.call(
            "context_get", project_id=self.project, task_id=first["id"],
            query="payroll ledger", budget=4000,
        )
        self.assertNotIn(candidate["id"], {item["id"] for item in unrelated["included_sources"]})
        self.assertNotIn("quasar refresh boundary sentinel", unrelated["text"])

        latest = self.call("task_read", task_id=first["id"])
        self.call(
            "task_close", task_id=first["id"], expected_revision=latest["revision"],
            expected_etag=latest["etag"], outcome="completed", summary="first task completed",
            acceptance_results={"phase review passes": "phase and knowledge review passed"},
            operation_id="close-first",
        )
        second = self.call(
            "task_open", project_id=self.project, title="unrelated payment work",
            goal="Work on payment reconciliation", acceptance=["payment regression"],
            operation_id="second-task",
        )
        self.assertEqual(second["status"], "current")
        reused = self.call(
            "context_get", project_id=self.project, task_id=second["id"],
            query="quasar", budget=4000,
        )
        self.assertIn(candidate["id"], {item["id"] for item in reused["included_sources"]})
        self.assertIn("quasar refresh boundary sentinel", reused["text"])

        active = self.call("memory_read", memory_id=candidate["id"])
        corrected = self.call(
            "memory_update",
            memory_id=candidate["id"],
            expected_revision=active["revision"],
            expected_etag=active["etag"],
            title="Refresh boundary knowledge corrected",
            body="nebula refresh boundary sentinel supersedes the old boundary rule.",
            operation_id="correct",
        )
        self.assertEqual(corrected["revision"], active["revision"] + 1)
        old_context = self.call(
            "context_get", project_id=self.project, task_id=second["id"],
            query="quasar", budget=4000,
        )
        self.assertNotIn(candidate["id"], {item["id"] for item in old_context["included_sources"]})
        self.assertNotIn("quasar refresh boundary sentinel", old_context["text"])
        new_context = self.call(
            "context_get", project_id=self.project, task_id=second["id"],
            query="nebula", budget=4000,
        )
        self.assertIn(candidate["id"], {item["id"] for item in new_context["included_sources"]})
        self.assertIn("nebula refresh boundary sentinel", new_context["text"])

        current_memory = self.call("memory_read", memory_id=candidate["id"])
        self.call(
            "memory_forget",
            memory_id=candidate["id"],
            expected_revision=current_memory["revision"],
            expected_etag=current_memory["etag"],
            operation_id="forget",
        )
        forgotten = self.call(
            "context_get", project_id=self.project, task_id=second["id"],
            query="nebula", budget=4000,
        )
        self.assertNotIn(candidate["id"], {item["id"] for item in forgotten["included_sources"]})
        self.assertNotIn("nebula refresh boundary sentinel", forgotten["text"])


if __name__ == "__main__":
    unittest.main()

