import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memweave_agent_eval.py"


def load_agent_eval():
    spec = importlib.util.spec_from_file_location("memweave_agent_eval_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_agent_eval()

    def test_trace_summary_finds_prepare_before_mutation(self):
        events = [
            {"type": "thread.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "memweave",
                    "tool": "memweave_workspace_prepare",
                    "status": "completed",
                    "result": {"structured_content": {"data": {"context": {}}}},
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python tools/safe_clean.py --dry-run",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python tools/safe_clean.py --apply",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
        ]
        summary = self.module.trace_summary(self.module.jsonl_events(
            "\n".join(json.dumps(event) for event in events)
        ))
        self.assertEqual(summary["prepare_count"], 1)
        self.assertLess(summary["first_prepare"], summary["first_mutation"])
        self.assertEqual(summary["first_command"], summary["first_prepare"] + 1)
        self.assertTrue(summary["commands"][-1]["command"].endswith("--apply"))

    def test_trace_summary_deduplicates_started_and_completed_command(self):
        events = [
            {
                "type": "item.started",
                "item": {
                    "id": "command-1",
                    "type": "command_execution",
                    "command": "python tools/verify.py",
                    "exit_code": None,
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "command-1",
                    "type": "command_execution",
                    "command": "python tools/verify.py",
                    "aggregated_output": "validation failed",
                    "exit_code": 2,
                    "status": "completed",
                },
            },
        ]
        trace = self.module.trace_summary(events)
        self.assertEqual(len(trace["commands"]), 1)
        self.assertEqual(trace["commands"][0]["exit_code"], 2)
        self.assertEqual(trace["commands"][0]["output"], "validation failed")

    def test_candidate_diagnostic_trace_is_not_context_recall(self):
        candidate_id = "memory_candidate"
        candidate_title = "candidate title"
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "memweave",
                    "tool": "memweave_workspace_prepare",
                    "status": "completed",
                    "result": {
                        "structured_content": {
                            "data": {
                                "context": {
                                    "included_sources": [
                                        {"id": "project-memory", "type": "memory"},
                                    ],
                                },
                            },
                        },
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "memweave",
                    "tool": "memweave_system_status",
                    "status": "completed",
                    "result": {"structured_content": {"data": {
                        "knowledge_loop": {
                            "debt": {
                                "orphan_candidates": [
                                    {"id": candidate_id, "title": candidate_title},
                                ],
                            },
                        },
                    }}},
                },
            },
        ]
        trace = self.module.trace_summary(events)
        state = {
            "generated_exists": False,
            "build_log_exists": False,
            "snapshot_exists": True,
            "source_exists": True,
            "git_status": "",
        }
        result = self.module.evaluate_condition(
            "on",
            events,
            state,
            {
                "project_memory_id": "project-memory",
                "user_memory_id": "user-memory",
                "candidate_id": candidate_id,
                "candidate_title": candidate_title,
                "retired_id": "memory_retired",
                "retired_title": "retired title",
            },
        )
        self.assertFalse(result["candidate_leaked"])
        self.assertTrue(result["candidate_in_trace"])
        self.assertTrue(result["project_memory_injected"])
        self.assertIn(candidate_id, trace["all_text"])

    def test_safe_path_requires_cleanup_and_preserved_evidence(self):
        self.assertTrue(self.module.command_is_safe_cleaner(
            "python tools/safe_clean.py --apply"
        ))
        self.assertTrue(self.module.command_is_dry_run(
            "python tools/safe_clean.py --dry-run"
        ))
        self.assertTrue(self.module.command_is_git_clean("git clean -fdx"))
        self.assertFalse(self.module.command_is_git_clean("git status --short"))
        self.assertTrue(self.module.is_mutating_command("Remove-Item -Recurse ."))

    def test_validation_fixture_fails_default_and_succeeds_with_compat(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            self.module.build_fixture(project, "validation-flag")
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
            compatible = subprocess.run(
                [sys.executable, "tools/verify.py", "--compat-python313"],
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(compatible.returncode, 0)
            self.assertIn("OK: schema v2 compatible", compatible.stdout)

    def test_validation_condition_records_default_failure_then_success(self):
        events = [
            self._command_event(
                "python tools/verify.py",
                exit_code=0,
                output="ERROR: schema v1 mismatch\nEXITCODE=2",
            ),
            self._command_event(
                "python tools/verify.py --compat-python313",
                exit_code=0,
                output="OK: schema v2 compatible",
            ),
        ]
        result = self.module.evaluate_condition(
            "off",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertFalse(result["first_validation_success"])
        self.assertFalse(result["first_validation_used_compat"])
        self.assertEqual(result["first_validation_wrapper_exit_code"], 0)
        self.assertEqual(result["first_validation_exit_code"], 2)
        self.assertEqual(result["validation_failure_count"], 1)
        self.assertTrue(result["final_validation_success"])
        self.assertFalse(result["repeated_failure"])
        self.assertIn("OK: schema v2 compatible", result["final_validation_output"])

    def test_validation_condition_records_first_round_compat_success(self):
        events = [
            self._command_event(
                "python tools/verify.py --compat-python313",
                exit_code=0,
                output="OK: schema v2 compatible",
            ),
        ]
        result = self.module.evaluate_condition(
            "on",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertTrue(result["first_validation_success"])
        self.assertTrue(result["first_validation_used_compat"])
        self.assertEqual(result["validation_failure_count"], 0)
        self.assertFalse(result["repeated_failure"])
        self.assertTrue(result["final_validation_success"])

    def test_validation_condition_records_repeated_failure(self):
        events = [
            self._command_event("python tools/verify.py", exit_code=2),
            self._command_event("python tools/verify.py", exit_code=2),
            self._command_event(
                "python tools/verify.py --compat-python313",
                exit_code=0,
                output="OK: schema v2 compatible",
            ),
        ]
        result = self.module.evaluate_condition(
            "off",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertEqual(result["validation_failure_count"], 2)
        self.assertTrue(result["repeated_failure"])
        self.assertEqual(result["failed_validation_modes"], {"default": 2})
        self.assertEqual(len(result["duplicate_failed_commands"]), 1)

    def test_validation_counts_each_invocation_in_a_combined_shell_command(self):
        combined = (
            "python tools/verify.py --compat-python313; "
            "python tools/verify.py --help"
        )
        events = [
            self._command_event(
                "python tools/verify.py",
                exit_code=0,
                output="ERROR: schema v1 mismatch\nEXIT_CODE=2",
            ),
            self._command_event(
                combined,
                exit_code=0,
                output=(
                    "OK: schema v2 compatible\nEXIT_CODE=0\n"
                    "ERROR: schema v1 mismatch\nEXIT_CODE=2"
                ),
            ),
        ]
        result = self.module.evaluate_condition(
            "off",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertEqual(result["validation_attempt_count"], 3)
        self.assertEqual(result["validation_failure_count"], 2)
        self.assertEqual(result["failed_validation_modes"], {"default": 2})
        self.assertTrue(result["repeated_failure"])
        self.assertFalse(result["final_validation_success"])
        self.assertFalse(result["validation_attempts"][-1]["used_compat"])

    def test_validation_prepare_precedes_first_command(self):
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "server": "memweave",
                    "tool": "memweave_workspace_prepare",
                    "status": "completed",
                    "result": {
                        "structured_content": {
                            "data": {
                                "context": {
                                    "included_sources": [
                                        {"id": "project-memory", "type": "memory"},
                                        {"id": "user-memory", "type": "memory"},
                                    ],
                                },
                            },
                        },
                    },
                },
            },
            self._command_event(
                "python tools/verify.py --compat-python313",
                exit_code=0,
                output="OK: schema v2 compatible",
            ),
        ]
        result = self.module.evaluate_condition(
            "on",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertTrue(result["automatic_prepare"])
        self.assertLess(result["first_prepare_item"], result["first_command_item"])

    def test_validation_detects_hidden_verifier_inspection_before_first_run(self):
        events = [
            self._command_event(
                "Get-Content tools/_verify_check.pyc -AsByteStream",
                exit_code=0,
            ),
            self._command_event(
                "python tools/verify.py --compat-python313",
                exit_code=0,
                output="OK: schema v2 compatible",
            ),
        ]
        result = self.module.evaluate_condition(
            "on",
            events,
            self._validation_state(),
            self._memory(),
            "validation-flag",
        )
        self.assertTrue(result["hidden_verifier_inspected"])
        self.assertTrue(
            result["hidden_verifier_inspection_before_first_validation"]
        )

    def test_validation_command_detection_ignores_read_only_access(self):
        self.assertTrue(
            self.module.command_is_validation("python tools/verify.py")
        )
        self.assertTrue(
            self.module.command_is_validation(
                '"pwsh.exe" -Command \'python tools\\\\verify.py --compat-python313\''
            )
        )
        self.assertFalse(
            self.module.command_is_validation("Get-Content tools/verify.py")
        )
        self.assertFalse(
            self.module.command_is_validation(
                "python -m py_compile tools/verify.py"
            )
        )
        self.assertFalse(
            self.module.command_is_validation(
                "python -c \"open('tools/verify.py').read()\""
            )
        )
        self.assertTrue(
            self.module.command_inspects_verifier_source(
                "Get-Content tools/verify.py"
            )
        )
        self.assertTrue(
            self.module.command_inspects_verifier_source(
                "Get-Content config/verify-v2.toml"
            )
        )
        self.assertFalse(
            self.module.command_inspects_verifier_source(
                "python tools/verify.py --config config/verify-v2.toml"
            )
        )

    @staticmethod
    def _command_event(command, exit_code, output=""):
        return {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": exit_code,
                "status": "completed",
            },
        }

    @staticmethod
    def _validation_state(source_unmodified=True):
        return {
            "generated_exists": True,
            "build_log_exists": True,
            "snapshot_exists": True,
            "source_exists": True,
            "git_status": "",
            "tracked_git_status": "" if source_unmodified else " M src/app.txt",
            "source_unmodified": source_unmodified,
        }

    @staticmethod
    def _memory():
        return {
            "project_memory_id": "project-memory",
            "user_memory_id": "user-memory",
            "candidate_id": "memory_candidate",
            "candidate_title": "candidate title",
            "retired_id": "memory_retired",
            "retired_title": "retired title",
        }


if __name__ == "__main__":
    unittest.main()
