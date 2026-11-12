"""Fast regression gate for the deterministic MemWeave memory-evaluation harness.

Run directly:

    python -m unittest tests.test_eval_smoke -v

Or as part of the repository suite:

    python -m unittest discover -s tests -v

The gate is deliberately small: two scenarios tagged ``smoke``, a registry guard
(P1/P2/P3 scenarios stay registered while heavy ones stay out of ``smoke``), one
repeat-based determinism check and one subprocess check of the documented CLI
entry point.  It never talks to a real model, never needs the network and never
touches ``%USERPROFILE%\\.memweave`` (all fixture homes live under the harness
work dir).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_LOADER_PATH = REPO_ROOT / "tests" / "eval" / "loader.py"
_spec = importlib.util.spec_from_file_location("memweave_eval_test_loader", _LOADER_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - broken checkout
    raise RuntimeError(f"cannot load evaluation test loader from {_LOADER_PATH}")
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


class EvalSmokeTests(unittest.TestCase):
    def setUp(self):
        self.module = loader.load_eval_module()
        self.work_root = loader.make_work_root(self)

    def test_smoke_scenarios_match_declared_a_b_c_expectations(self):
        report = self.module.run_scenarios(tags=["smoke"], work_root=self.work_root)
        self.assertEqual(report["schema_version"], 1)
        self.assertGreaterEqual(
            len(report["scenarios"]), 2, "smoke tag must cover at least two scenarios",
        )
        for scenario in report["scenarios"]:
            self.assertEqual(
                scenario["status"], "pass",
                f"{scenario['id']}: {scenario.get('error')}",
            )
            self.assertEqual(set(scenario["conditions"]), {"A", "B", "C"})
            self.assertEqual(
                scenario["conditions"]["A"]["state"], "registration_required",
                "condition A must run against an empty MemWeave home",
            )
            self.assertEqual(scenario["conditions"]["A"]["context_chars"], 0)
            self.assertGreater(scenario["conditions"]["B"]["context_chars"], 0)
            self.assertGreater(scenario["conditions"]["C"]["context_chars"], 0)
            self.assertEqual(
                scenario["conditions"]["A"]["knowledge_injected"], False,
            )

        task_gain = report["summary"]["recall_sets"]["task_memory_gain"]
        self.assertGreater(task_gain["by_condition"]["B"]["total"], 0)
        self.assertEqual(task_gain["by_condition"]["A"]["hits"], 0)
        self.assertEqual(task_gain["by_condition"]["B"]["rate"], 1.0)
        knowledge_gain = report["summary"]["recall_sets"]["long_term_knowledge_gain"]
        self.assertGreater(knowledge_gain["by_condition"]["C"]["total"], 0)
        self.assertEqual(knowledge_gain["by_condition"]["B"]["hits"], 0)
        self.assertEqual(knowledge_gain["by_condition"]["C"]["rate"], 1.0)
        self.assertEqual(
            report["summary"]["scenarios_failed"] + report["summary"]["scenarios_errored"],
            0,
        )
        # The report is what --json emits; it must stay JSON-serializable.
        json.dumps(report)

    def test_scenario_registry_covers_p1_to_p3_and_keeps_smoke_small(self):
        # Loading the harness is cheap; executing the non-smoke scenarios is not.
        # This guard catches an accidentally dropped/renamed scenario or a new
        # heavy scenario leaking into the fast smoke gate.
        scenario_ids = [scenario.id for scenario in self.module.SCENARIOS]
        self.assertGreaterEqual(len(scenario_ids), 10, scenario_ids)
        for scenario_id in (
            "evidence_pointer_persistence",
            "consolidation_lint_and_truncation",
            "knowledge_index_injection",
        ):
            self.assertIn(scenario_id, scenario_ids)
        smoke_ids = sorted(
            scenario.id for scenario in self.module.SCENARIOS if "smoke" in scenario.tags
        )
        self.assertEqual(smoke_ids, ["long_task_recovery", "stale_knowledge_trap"])
        for scenario in self.module.SCENARIOS:
            self.assertTrue(callable(scenario.build), scenario.id)
            self.assertTrue(callable(scenario.make_checks), scenario.id)

    def test_repeat_run_produces_identical_deterministic_facts(self):
        report = self.module.run_scenarios(
            ids=["long_task_recovery"], work_root=self.work_root, repeat=2,
        )
        scenario = report["scenarios"][0]
        self.assertEqual(
            scenario["status"], "pass", scenario.get("error"),
        )
        self.assertEqual(
            scenario["reproducibility"], {"repeat": 2, "identical": True},
        )
        self.assertEqual(len(report["summary"]["failing_cells"]), 0)

    def test_cli_json_entrypoint(self):
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                str(loader.EVAL_SCRIPT),
                "--scenario", "long_task_recovery",
                "--json",
                "--workdir", str(self.work_root),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        report = json.loads(result.stdout)
        self.assertEqual(len(report["scenarios"]), 1)
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["id"], "long_task_recovery")
        self.assertEqual(scenario["status"], "pass", scenario.get("error"))
        self.assertEqual(scenario["conditions"]["A"]["state"], "registration_required")
        for condition in ("A", "B", "C"):
            self.assertIn("context_chars", scenario["conditions"][condition])
            self.assertIn("surface_calls", scenario["conditions"][condition])
        self.assertIn("recall_sets", report["summary"])
        self.assertIn("cost", report["summary"])


if __name__ == "__main__":
    unittest.main()
