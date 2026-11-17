import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memweave_closed_loop_eval.py"


def load_closed_loop_eval():
    spec = importlib.util.spec_from_file_location(
        "memweave_closed_loop_eval_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClosedLoopEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_closed_loop_eval()

    def test_correction_is_the_only_prompt_that_reveals_validation_key(self):
        key = "p25-test-secret"
        self.assertNotIn(
            key,
            self.module.LEARN_INITIAL_PROMPT,
        )
        self.assertNotIn(
            key,
            self.module.P25_VERIFY_PROMPT,
        )
        self.assertIn(
            key,
            self.module.learn_correction_prompt(key),
        )

    def test_thread_id_and_mcp_tool_names_are_extracted(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-123"},
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "memweave_task_open",
                    "status": "completed",
                },
            },
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "memweave_task_close",
                    "status": "in_progress",
                },
            },
        ]
        self.assertEqual(
            self.module.thread_id_from_events(events),
            "thread-123",
        )
        self.assertEqual(
            self.module.mcp_tool_names(events),
            ["memweave_task_open"],
        )

    def test_key_fixture_really_validates_default_and_signed_profile(self):
        key = "p25-test-secret"
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            self.module.build_fixture(
                project,
                "validation-key",
                validation_key=key,
            )
            default = subprocess.run(
                [sys.executable, "tools/verify.py"],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(default.returncode, 2)
            self.assertIn("schema v1 mismatch", default.stderr)
            corrected = subprocess.run(
                [
                    sys.executable,
                    "tools/verify.py",
                    "--validation-key",
                    key,
                ],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(corrected.returncode, 0)
            self.assertIn("OK: schema v2 compatible", corrected.stdout)
            invocations = (
                project / "artifacts" / "verify-invocations.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(invocations), 2)
            self.assertEqual(json.loads(invocations[0])["args"], [])
            self.assertEqual(
                json.loads(invocations[1])["args"],
                ["--validation-key", key],
            )

    def test_learning_evidence_requires_review_and_active_key_memory(self):
        before = {"active_project_memory_ids": []}
        after = {
            "reviewed_task_ids": ["task-1"],
            "active_correct_key_memory_ids": ["memory-1"],
            "active_correct_key_memory_titles": ["验证必须使用签名 key"],
            "active_user_first_rule_ids": ["memory-user-1"],
            "open_task_ids": [],
        }
        tool_names = [
            "memweave_task_open",
            "memweave_task_close",
            "memweave_memory_write",
            "memweave_memory_review",
            "memweave_memory_review_complete",
        ]
        initial = {
            "first_validation_success": False,
            "validation_failure_count": 1,
            "first_validation_used_compat": False,
            "hidden_verifier_inspected": False,
        }
        correction = {
            "first_validation_success": True,
            "first_validation_used_compat": True,
            "first_validation_used_validation_key": True,
        }
        project_state = {
            "source_unmodified": True,
            "git_status": "?? AGENTS.md\n!! .agents/\n!! .codex/\n",
        }
        result = self.module.learning_evidence(
            before,
            after,
            tool_names,
            initial,
            correction,
            project_state,
        )
        self.assertTrue(result["persisted"])
        self.assertEqual(result["issues"], [])

        missing_review = self.module.learning_evidence(
            before,
            after,
            [name for name in tool_names if name != "memweave_memory_review_complete"],
            initial,
            correction,
            project_state,
        )
        self.assertFalse(missing_review["persisted"])
        self.assertTrue(
            any("review_complete" in issue for issue in missing_review["issues"])
        )

    def test_learning_evidence_rejects_unexpected_project_memory_file(self):
        result = self.module.learning_evidence(
            {"active_project_memory_ids": []},
            {
                "reviewed_task_ids": ["task-1"],
                "active_correct_key_memory_ids": ["memory-1"],
                "active_correct_key_memory_titles": ["验证必须使用签名 key"],
                "active_user_first_rule_ids": ["memory-user-1"],
                "open_task_ids": [],
            },
            [
                "memweave_task_open",
                "memweave_task_close",
                "memweave_memory_write",
                "memweave_memory_review",
                "memweave_memory_review_complete",
            ],
            {
                "first_validation_success": False,
                "validation_failure_count": 1,
                "first_validation_used_compat": False,
                "hidden_verifier_inspected": False,
            },
            {
                "first_validation_success": True,
                "first_validation_used_compat": True,
                "first_validation_used_validation_key": True,
            },
            {
                "source_unmodified": True,
                "git_status": "?? AGENTS.md\n?? VERIFY_NOTES.md\n",
            },
        )
        self.assertFalse(result["persisted"])
        self.assertEqual(
            result["unexpected_untracked_files"],
            ["VERIFY_NOTES.md"],
        )

    def test_behavior_requires_control_failure_injection_and_first_success(self):
        learning = {"persisted": True}
        control = {
            "first_validation_success": False,
            "validation_failure_count": 1,
            "included_memory_ids": [],
        }
        reuse = {
            "automatic_prepare": True,
            "first_validation_success": True,
            "first_validation_used_compat": True,
            "first_validation_used_validation_key": True,
            "validation_failure_count": 0,
            "repeated_failure": False,
            "final_validation_success": True,
            "project_memory_injected": True,
            "user_memory_injected": True,
            "hidden_verifier_inspection_before_first_validation": False,
            "verifier_source_inspection_before_first_validation": False,
            "source_unmodified": True,
        }
        result = self.module.assess_behavior(
            learning,
            control,
            reuse,
            injected_body_has_validation_key=True,
        )
        self.assertTrue(result["criterion_met"])

        not_attributable = self.module.assess_behavior(
            learning,
            control,
            {
                **reuse,
                "hidden_verifier_inspection_before_first_validation": True,
            },
            injected_body_has_validation_key=True,
        )
        self.assertFalse(not_attributable["criterion_met"])
        self.assertFalse(not_attributable["reuse_attributable"])

    def test_prepare_text_must_contain_the_learned_key(self):
        key = "p25-test-secret"
        self.assertTrue(
            self.module.prepare_text_has_validation_key(
                {
                    "prepare_results": [
                        {
                            "data": {
                                "context": {
                                    "text": (
                                        "run python tools/verify.py "
                                        f"--validation-key {key}"
                                    ),
                                },
                            },
                        },
                    ],
                },
                key,
            )
        )
        self.assertFalse(
            self.module.prepare_text_has_validation_key(
                {
                    "prepare_results": [
                        {
                            "data": {
                                "context": {
                                    "text": "run python tools/verify.py",
                                },
                            },
                        },
                    ],
                },
                key,
            )
        )

    def test_report_json_is_serializable_for_snapshots(self):
        payload = {
            "threads": {"learn": "thread-1"},
            "prepare_results": [{"data": {"nested": {"value": 1}}}],
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("thread-1", encoded)


if __name__ == "__main__":
    unittest.main()
