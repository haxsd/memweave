import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.service.start()
        self.addCleanup(self.service.close)
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
                  findings="LATEST_CORRECTION_DO_NOT_ROLL_BACK " + "detail " * 600, progress="Handler located",
                  next_step="Run expiry regression", operation_id="checkpoint")
        context = self.call("context_get", project_id=self.project, task_id=task["id"], query="auth", budget=2400)
        self.assertIn("Run expiry regression", context["text"])
        self.assertIn("Keep refresh tokens", context["text"])
        self.assertIn("auth.py", context["text"])
        self.assertIn("auth refresh boundary", context["text"])
        self.assertLessEqual(len(context["text"]), 2400)
        self.assertIn("LATEST_CORRECTION_DO_NOT_ROLL_BACK", context["text"])
        self.assertTrue({task["id"], memory["id"], architecture["id"], "file:auth.py"}.issubset(
            {source["id"] for source in context["included_sources"]}
        ))
        self.assertEqual(context["graph"]["freshness"]["state"], "checked")
        default = self.call("context_get", project_id=self.project, task_id=task["id"], budget=2400)
        self.assertIn("LATEST_CORRECTION_DO_NOT_ROLL_BACK", default["text"])
        self.assertNotIn("Structure [", default["text"])
        self.assertNotIn("History:", default["text"])

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

    def test_context_does_not_inject_recent_memory_when_query_misses(self):
        continuation = self.call(
            "memory_write",
            project_id=self.project,
            title="任务接续方法",
            body="切换工具前保存检查点，恢复时读取当前任务和下一步。",
            sources=[{"type": "user", "reference": "输入"}],
            operation_id="continuation",
        )
        self.call(
            "memory_write",
            project_id=self.project,
            title="验收候选知识",
            body="候选知识经核对并明确启用后，才进入常规检索与上下文。",
            sources=[{"type": "host", "reference": "验收"}],
            operation_id="acceptance",
        )
        context = self.call(
            "context_get",
            project_id=self.project,
            query="resume previous work in another client",
            budget=4000,
        )
        sources = {source["id"]: source for source in context["included_sources"]}
        self.assertNotIn(continuation["id"], sources)
        self.assertNotIn("任务接续方法", context["text"])
        self.assertFalse(context["memory"]["recent_fallback"])
        self.assertEqual(context["memory"]["strategy"], "basic_memory_sqlite_fts5")

    def test_task_title_does_not_inject_unrelated_project_knowledge(self):
        task = self.call(
            "task_open", project_id=self.project, title="auth task", goal="Fix auth",
            acceptance=["verified"], operation_id="task",
        )
        self.call(
            "memory_write", project_id=self.project, title="unrelated project note",
            body="This should require a matching query.",
            sources=[{"type": "user", "reference": "explicit"}], operation_id="project-memory",
        )
        context = self.call("context_get", project_id=self.project, task_id=task["id"])
        self.assertNotIn("unrelated project note", context["text"])
        self.assertFalse(context["memory"]["recent_fallback"])

    def test_memory_search_returns_retrieval_and_context_metadata(self):
        memory = self.call(
            "memory_write", project_id=self.project, title="Basic Memory context boundary",
            body="The search row is the only canonical retrieval result.",
            sources=[{"type": "host", "reference": "test"}], operation_id="retrieval",
        )
        hit = self.call("memory_search", project_id=self.project, query="canonical retrieval")["items"][0]
        self.assertEqual(hit["id"], memory["id"])
        self.assertTrue(hit["retrieval"]["permalink"])
        detail = self.service.memory_context(hit["retrieval"]["permalink"])
        self.assertEqual(detail["metadata"]["primary_count"], 1)
        with patch.object(self.service, "memory_context", side_effect=AssertionError("unused relation lookup")):
            context = self.call(
                "context_get", project_id=self.project, query="canonical retrieval", budget=3000
            )
        self.assertEqual(context["memory"]["strategy"], "basic_memory_sqlite_fts5")
        self.assertIn(memory["id"], {source["id"] for source in context["included_sources"]})

    def test_architecture_is_prioritized_before_graph_result_limit(self):
        for position in range(45):
            (self.root / f"auth_{position}.py").write_text("pass", encoding="utf-8")
        architecture = self.map_architecture()
        context = self.call("context_get", project_id=self.project, query="auth")
        self.assertIn(architecture["id"], {source["id"] for source in context["included_sources"]})
        self.assertIn("host_reported", context["text"])

    def test_tight_budget_keeps_unique_next_step_and_acceptance_visible(self):
        task = self.call(
            "task_open", project_id=self.project, title="tight budget continuity",
            goal=(
                "Preserve the unique next step and acceptance even when the context "
                "budget is only 500 characters. " * 4
            ),
            acceptance=["acceptance alpha visible", "acceptance beta visible"],
            operation_id="tight-task",
        )
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=1,
            expected_etag=task["etag"], findings="Located the boundary. " * 60,
            progress="Implementation underway", next_step="NEXT_ACTION_UNDER_PRESSURE",
            operation_id="tight-checkpoint",
        )
        self.map_architecture()
        for position in range(10):
            self.call(
                "memory_write", project_id=self.project, title=f"tight rule {position}",
                body="Keep refresh tokens outside browser scripts. " * 40,
                sources=[{"type": "user", "reference": "explicit"}],
                operation_id=f"tight-memory-{position}",
            )
        context = self.call(
            "context_get", project_id=self.project, task_id=task["id"],
            query="tight", budget=500,
        )
        text = context["text"]
        self.assertLessEqual(len(text), 500)
        self.assertIn("NEXT_ACTION_UNDER_PRESSURE", text)
        self.assertIn("acceptance alpha visible", text)
        self.assertIn("acceptance beta visible", text)
        self.assertIn("Preserve the unique next step", text)
        self.assertIn(task["id"], {source["id"] for source in context["included_sources"]})
        self.assertTrue(context["truncated"])

        # A verbose execution-root header must not consume the task brief floor.
        nested = self.root / "src"
        nested.mkdir()
        nested_context = self.call(
            "context_get", project_id=self.project, task_id=task["id"],
            query="tight", budget=500, execution_root=str(nested),
        )
        self.assertLessEqual(len(nested_context["text"]), 500)
        self.assertIn("NEXT_ACTION_UNDER_PRESSURE", nested_context["text"])
        self.assertIn("acceptance alpha visible", nested_context["text"])

    def test_task_and_open_work_units_keep_priority_in_a_small_budget(self):
        task = self.call(
            "task_open", project_id=self.project, title="auth continuity",
            goal="Preserve the current task boundary. " * 80,
            acceptance=["current next step remains visible"], operation_id="task",
        )
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=1,
            expected_etag=task["etag"], findings="Located the auth boundary. " * 80,
            progress="Parallel checks are running", next_step="Run targeted regression",
            operation_id="checkpoint",
        )
        unit = self.call(
            "work_unit_update", task_id=task["id"], title="blocked integration",
            owner="worker-blocked", status="running", progress="waiting on contract",
            operation_id="unit",
        )
        unit = self.call(
            "work_unit_update", unit_id=unit["id"], expected_revision=unit["revision"],
            expected_etag=unit["etag"], status="blocked", blocker="Contract is unconfirmed",
            next_step="Confirm the current contract", operation_id="unit-blocked",
        )
        for position in range(12):
            self.call(
                "memory_write", project_id=self.project, title=f"auth memory {position}",
                body="An older auth detail. " * 120,
                sources=[{"type": "user", "reference": "explicit"}],
                operation_id=f"memory-{position}",
            )
        context = self.call(
            "context_get", project_id=self.project, task_id=task["id"],
            query="auth", budget=1200,
        )
        included = {source["id"] for source in context["included_sources"]}
        self.assertIn("Run targeted regression", context["text"])
        self.assertIn("worker-blocked", context["text"])
        self.assertIn("Contract is unconfirmed", context["text"])
        self.assertIn(unit["id"], included)
        self.assertTrue(context["truncated"])

    def test_reboot_summary_exposes_five_questions_and_blocked_completion(self):
        task = self.call(
            "task_open", project_id=self.project, title="auth continuity",
            goal="Keep the current task recoverable.",
            acceptance=["next step remains visible"], operation_id="reboot-task",
        )
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=1,
            expected_etag=task["etag"], findings="Located the boundary.",
            failures="First attempt missed the expiry check.",
            progress="Implementation is in progress.", next_step="Run targeted regression",
            operation_id="reboot-checkpoint",
        )
        unit = self.call(
            "work_unit_update", task_id=task["id"], title="blocked integration",
            owner="worker-blocked", status="running", progress="waiting on contract",
            operation_id="reboot-unit",
        )
        unit = self.call(
            "work_unit_update", unit_id=unit["id"], expected_revision=unit["revision"],
            expected_etag=unit["etag"], status="blocked", blocker="Contract is unconfirmed",
            next_step="Confirm the current contract", operation_id="reboot-unit-blocked",
        )
        context = self.call("context_get", project_id=self.project, task_id=task["id"])
        reboot = context["reboot"]
        self.assertEqual(set(reboot), {
            "where", "goal", "findings", "done", "failed", "next", "contract", "attention", "completion", "text",
        })
        self.assertIn("Run targeted regression", reboot["next"])
        self.assertIn("Located the boundary.", reboot["findings"])
        self.assertIn("Contract is unconfirmed", reboot["failed"])
        self.assertIn("Reboot summary:", reboot["text"])
        self.assertIn("Completion: blocked", reboot["text"])
        self.assertEqual(context["completion"]["state"], "blocked")
        self.assertEqual(context["completion"]["blocked_work_unit_ids"], [unit["id"]])
        self.assertEqual(reboot["contract"]["state"], "attested")
        self.assertIn("blocked_work_units", {item["code"] for item in context["attention"]})

    def test_current_task_keeps_acceptance_unreported_despite_claimed_progress(self):
        task = self.call(
            "task_open", project_id=self.project, title="Native model support",
            goal="Two native selectors work", acceptance=["IDE selector works", "Agent selector works"],
            operation_id="native-task",
        )
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="Unit tests pass; real UI is unverified.",
            progress="All implementation steps complete", verification="100 unit tests passed",
            next_step="Verify both real selectors", operation_id="native-progress",
        )
        context = self.call("context_get", project_id=self.project, task_id=task["id"], budget=4000)
        self.assertEqual(context["completion"]["acceptance_reported_count"], 0)
        self.assertEqual(context["completion"]["acceptance_unreported_count"], 2)
        self.assertIn("Acceptance (not yet reported)", context["text"])
        self.assertIn("IDE selector works", context["text"])
        self.assertIn("Agent selector works", context["text"])
        self.assertEqual(context["text"].count("Task [" + task["id"] + "]"), 1)

    def test_task_contract_attestation_detects_external_drift(self):
        task = self.call(
            "task_open", project_id=self.project, title="contract task",
            goal="Keep the task contract stable.",
            acceptance=["contract remains stable"], operation_id="contract-task",
        )
        task = self.call("task_read", task_id=task["id"])
        self.assertTrue(task["contract_hash"])
        context = self.call("context_get", project_id=self.project, task_id=task["id"])
        self.assertEqual(context["reboot"]["contract"]["state"], "attested")
        self.assertNotIn("task_contract_drift", {item["code"] for item in context["attention"]})

        path = Path(self.service.vault.home) / task["path"]
        text = path.read_text(encoding="utf-8")
        old = '"goal": "Keep the task contract stable."'
        self.assertIn(old, text)
        path.write_text(
            text.replace(old, '"goal": "Changed outside the task lifecycle."', 1),
            encoding="utf-8",
        )
        drifted = self.call("context_get", project_id=self.project, task_id=task["id"])
        self.assertEqual(drifted["reboot"]["contract"]["state"], "drift")
        self.assertIn("task_contract_drift", {item["code"] for item in drifted["attention"]})

        task = self.call("task_read", task_id=task["id"])
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=task["revision"],
            expected_etag=task["etag"], findings="", progress="Contract change confirmed",
            next_step="Continue with the confirmed contract", attest_contract=True,
            operation_id="contract-reattest",
        )
        reattested = self.call("context_get", project_id=self.project, task_id=task["id"])
        self.assertEqual(reattested["reboot"]["contract"]["state"], "attested")
        self.assertNotIn("task_contract_drift", {item["code"] for item in reattested["attention"]})

    def test_context_attention_marks_missing_next_step_and_stale_graph(self):
        self.map_architecture()
        task = self.call(
            "task_open", project_id=self.project, title="unplanned task",
            goal="Exercise attention signals.", acceptance=["signal visible"],
            operation_id="attention-task",
        )
        self.source.write_text("def refresh(): return 'changed'", encoding="utf-8")
        context = self.call("context_get", project_id=self.project, task_id=task["id"])
        codes = {item["code"] for item in context["attention"]}
        self.assertIn("next_step_missing", codes)
        self.assertIn("graph_stale", codes)
        self.assertEqual(context["completion"]["state"], "ready")
        self.assertIn("next_step_missing", context["reboot"]["text"])

    def test_context_is_project_scoped_and_stable_after_rebuild(self):
        other_root = Path(self.temporary.name) / "other"
        other_root.mkdir()
        other_project = self.call(
            "project_register", root=str(other_root), name="Other",
            operation_id="other-project",
        )["id"]
        own_memory = self.call(
            "memory_write", project_id=self.project, title="auth boundary",
            body="Current project validates refresh expiry at the boundary.",
            sources=[{"type": "host", "reference": "test"}], operation_id="own-memory",
        )
        self.call(
            "memory_write", project_id=other_project, title="other boundary",
            body="auth zephyr marker belongs to another project.",
            sources=[{"type": "host", "reference": "test"}], operation_id="other-memory",
        )
        before = self.call(
            "context_get", project_id=self.project, query="auth boundary", budget=3000
        )
        self.assertIn(own_memory["id"], {source["id"] for source in before["included_sources"]})
        self.assertNotIn("zephyr marker", before["text"])
        other_context = self.call(
            "context_get", project_id=other_project, query="auth boundary", budget=3000
        )
        self.assertNotIn(own_memory["id"], {source["id"] for source in other_context["included_sources"]})
        self.assertNotIn("Current project validates", other_context["text"])

        rebuilt = self.service.rebuild()

        self.assertEqual(rebuilt["search_index"]["failed"], [])
        after = self.call(
            "context_get", project_id=self.project, query="auth boundary", budget=3000
        )
        self.assertEqual(
            [source["id"] for source in before["included_sources"]],
            [source["id"] for source in after["included_sources"]],
        )
        self.assertEqual(
            before["memory"]["recent_fallback"], after["memory"]["recent_fallback"]
        )
        self.assertFalse(after["memory"]["recent_fallback"])

    def test_knowledge_index_injection_is_off_by_default_and_byte_identical(self):
        self.call(
            "memory_write", project_id=self.project, title="ACTIVE_ONLY_NAV",
            body="Navigation row that must stay out of the default context.",
            sources=[{"type": "user", "reference": "explicit"}], operation_id="default-index",
        )
        task = self.call(
            "task_open", project_id=self.project, title="default index task",
            goal="keep default context unchanged", acceptance=["no index by default"],
            operation_id="default-index-task",
        )
        parameters = dict(project_id=self.project, task_id=task["id"], query="auth", budget=4000)
        baseline = self.call("context_get", **parameters)
        explicit_off = self.call("context_get", **parameters, include_knowledge_index=False)
        self.assertEqual(
            json.dumps(baseline, ensure_ascii=False, sort_keys=True),
            json.dumps(explicit_off, ensure_ascii=False, sort_keys=True),
        )
        self.assertNotIn("knowledge_index", baseline)
        self.assertNotIn("Knowledge index", baseline["text"])
        with patch.object(
            self.service, "knowledge_index",
            side_effect=AssertionError("knowledge index must stay lazy when disabled"),
        ), patch(
            "memweave.context.knowledge_index_block",
            side_effect=AssertionError("knowledge index block must stay lazy when disabled"),
        ):
            still_default = self.call("context_get", **parameters)
        self.assertEqual(still_default, baseline)
        self.assertEqual(
            json.dumps(still_default, ensure_ascii=False, sort_keys=True),
            json.dumps(baseline, ensure_ascii=False, sort_keys=True),
        )

    def test_knowledge_index_injection_is_active_only_and_bounded(self):
        candidate = self.call(
            "memory_write", project_id=self.project, title="CANDIDATE_ONLY_NAV",
            body="Candidate navigation row must never be injected.",
            sources=[{"type": "host", "reference": "seed"}], status="candidate",
            operation_id="candidate-nav",
        )
        retired_source = self.call(
            "memory_write", project_id=self.project, title="RETIRED_ONLY_NAV",
            body="Retired navigation row must never be injected.",
            sources=[{"type": "host", "reference": "seed"}], operation_id="retired-nav-write",
        )
        self.call(
            "memory_update", memory_id=retired_source["id"],
            expected_revision=retired_source["revision"], expected_etag=retired_source["etag"],
            title="RETIRED_ONLY_NAV", body="Retired navigation row must never be injected.",
            status="retired", operation_id="retired-nav",
        )
        for position in range(35):
            self.call(
                "memory_write", project_id=self.project, title=f"bulk active {position:02d}",
                body="Bulk navigation entry body.", sources=[{"type": "host", "reference": "seed"}],
                operation_id=f"bulk-nav-{position:02d}",
            )
        newest = self.call(
            "memory_write", project_id=self.project, title="ACTIVE_NEWEST_NAV",
            body="Newest navigation row.", sources=[{"type": "host", "reference": "seed"}],
            operation_id="active-newest-nav",
        )
        context = self.call(
            "context_get", project_id=self.project, query="auth", budget=12000,
            include_knowledge_index=True,
        )
        text = context["text"]
        self.assertLessEqual(len(text), 12000)
        self.assertIn("Knowledge index (navigation only; not evidence)", text)
        self.assertIn("ACTIVE_NEWEST_NAV", text)
        self.assertIn(newest["id"], text)
        self.assertNotIn("CANDIDATE_ONLY_NAV", text)
        self.assertNotIn(candidate["id"], text)
        self.assertNotIn("RETIRED_ONLY_NAV", text)
        metadata = context["knowledge_index"]
        self.assertTrue(metadata["requested"])
        self.assertTrue(metadata["injected"])
        self.assertEqual(metadata["active_total"], 36)
        self.assertEqual(metadata["line_limit"], 30)
        self.assertEqual(metadata["char_limit"], 4000)
        self.assertGreaterEqual(metadata["lines"], 1)
        self.assertLessEqual(metadata["lines"], 30)
        self.assertLessEqual(metadata["characters"], 4000)
        self.assertTrue(metadata["truncated"])
        self.assertIn("truncated", text)
        start = text.index("Knowledge index (navigation only; not evidence)")
        end = text.find("\n\n", start)
        block = text[start:] if end == -1 else text[start:end]
        self.assertEqual(len(block), metadata["characters"])

    def test_knowledge_index_injection_protects_the_task_floor(self):
        task = self.call(
            "task_open", project_id=self.project, title="budget index task",
            goal="Preserve the goal and next action at a tiny budget. " * 8,
            acceptance=["TINY_BUDGET_ACCEPTANCE"], operation_id="tiny-index-task",
        )
        self.call(
            "task_checkpoint", task_id=task["id"], expected_revision=1,
            expected_etag=task["etag"], findings="Located evidence. " * 40,
            progress="Working", next_step="NEXT_STEP_INDEX_500",
            operation_id="tiny-index-checkpoint",
        )
        for position in range(20):
            self.call(
                "memory_write", project_id=self.project, title=f"tiny index entry {position}",
                body="Long navigation body so the index would consume budget. " * 6,
                sources=[{"type": "user", "reference": "explicit"}],
                operation_id=f"tiny-index-{position}",
            )
        context = self.call(
            "context_get", project_id=self.project, task_id=task["id"],
            query="tiny", budget=500, include_knowledge_index=True,
        )
        text = context["text"]
        self.assertLessEqual(len(text), 500)
        self.assertIn("NEXT_STEP_INDEX_500", text)
        self.assertIn("TINY_BUDGET_ACCEPTANCE", text)
        self.assertIn("Preserve the goal and next action", text)
        self.assertTrue(context["knowledge_index"]["requested"])
        self.assertFalse(context["knowledge_index"]["injected"])
        self.assertEqual(context["knowledge_index"]["omitted_reason"], "budget")
        default = self.call(
            "context_get", project_id=self.project, task_id=task["id"], query="tiny", budget=500,
        )
        self.assertEqual(text, default["text"])

    def test_include_knowledge_index_rejects_non_boolean_values(self):
        for value in ("true", 1, 0, None, [], {"enabled": True}):
            with self.subTest(value=value), self.assertRaises(DiagnosticError) as caught:
                self.call("context_get", project_id=self.project, include_knowledge_index=value)
            self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def test_invalid_task_ids_cannot_silently_disable_task_context(self):
        for invalid in ("", False, 0, []):
            with self.subTest(task_id=invalid), self.assertRaises(DiagnosticError):
                self.call("context_get", project_id=self.project, task_id=invalid)


if __name__ == "__main__":
    unittest.main()
