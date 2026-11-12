import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memweave.diagnostics import DiagnosticError
from memweave.storage import (
    MAX_EVIDENCE_ITEMS,
    Vault,
    normalize_evidence,
    record_evidence,
)


def sample_evidence():
    return [
        {
            "kind": "command",
            "reference": "python -m unittest tests.test_storage",
            "hash": "abc123",
            "observed_at": "2026-11-08T10:00:00+08:00",
            "summary": "focused suite passed",
        },
        {"kind": "file", "reference": "src/memweave/storage.py", "hash": "sha256:deadbeef"},
        {"kind": "user", "reference": "用户要求证据可回放"},
    ]


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Vault(Path(self.temporary.name) / "织忆 库")
        self.vault.initialize()

    def record(self, record_id="memory_one", body="可复用的中文知识"):
        return {
            "id": record_id, "type": "memory", "scope": "project",
            "project_id": "project_one", "title": "认证约束", "body": body,
            "status": "active", "revision": 1,
        }

    def create(self, record=None, operation_id="create-one"):
        record = record or self.record()
        return self.vault.commit(
            operation_id, {"action": "create", "record": record},
            lambda: ([{"record": record, "path": f"records/{record['id']}.md", "expected": None}],
                     {"id": record["id"]}),
        )

    def test_markdown_is_authoritative_and_survives_new_instances(self):
        self.create()
        restored = Vault(self.vault.home).read("memory_one")
        self.assertEqual(restored["body"], "可复用的中文知识")
        markdown = (self.vault.home / restored["path"]).read_text("utf-8")
        self.assertIn("---\n", markdown)
        self.assertIn("可复用的中文知识", markdown)
        self.assertEqual(restored["etag"], hashlib.sha256(markdown.encode()).hexdigest())

    def test_retry_returns_original_result_and_changed_payload_conflicts(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        with self.assertRaises(DiagnosticError) as caught:
            self.create(self.record(body="different"))
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        self.assertEqual(len(self.vault.list_records("memory")), 1)

    def test_external_edit_changes_etag_and_old_writer_is_rejected(self):
        self.create()
        old = self.vault.read("memory_one")
        target = self.vault.home / old["path"]
        target.write_text(target.read_text("utf-8").replace("可复用的中文知识", "用户直接修订"), "utf-8")
        self.assertEqual(self.vault.read("memory_one")["body"], "用户直接修订")
        updated = {**self.record(), "revision": 2}
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.commit("update-one", {"update": "old"}, lambda: (
                [{"record": updated, "path": old["path"], "expected": old["etag"]}], {"id": old["id"]}
            ))
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        self.assertEqual(self.vault.read("memory_one")["body"], "用户直接修订")

    def test_prepared_operation_recovers_without_duplicating_records(self):
        with patch.object(self.vault, "_apply_change", side_effect=RuntimeError("power loss")):
            with self.assertRaises(RuntimeError):
                self.create()
        restored = Vault(self.vault.home)
        self.assertEqual(restored.recover()["recovered"], 1)
        self.assertEqual(restored.read("memory_one")["body"], "可复用的中文知识")
        self.assertEqual(self.create(), {"id": "memory_one"})
        self.assertEqual(restored.recover()["recovered"], 0)

    def test_read_and_list_recover_a_pending_write_after_writer_exit(self):
        self.create()
        with patch.object(self.vault, "_apply_change", side_effect=RuntimeError("writer exit")):
            with self.assertRaises(RuntimeError):
                self.vault.commit("pending-update", {"update": True}, lambda: (
                    [{
                        "record": {**self.record(body="recovered content"), "revision": 2},
                        "path": "records/memory_one.md",
                        "expected": self.vault.read("memory_one")["etag"],
                    }],
                    {"id": "memory_one"},
                ))
        restarted = Vault(self.vault.home)
        self.assertEqual(restarted.read("memory_one")["body"], "recovered content")
        self.assertEqual([item["id"] for item in restarted.list_records("memory")], ["memory_one"])
        self.assertEqual(restarted.recovery_status()["items"], [])

    def test_recovery_conflict_preserves_external_content(self):
        self.create()
        old = self.vault.read("memory_one")
        with patch.object(self.vault, "_apply_change", side_effect=RuntimeError("power loss")):
            with self.assertRaises(RuntimeError):
                self.vault.commit("update", {"update": True}, lambda: (
                    [{"record": {**self.record(), "revision": 2}, "path": old["path"], "expected": old["etag"]}],
                    {"id": old["id"]},
                ))
        target = self.vault.home / old["path"]
        target.write_text("external content", "utf-8")
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.recover()
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(target.read_text("utf-8"), "external content")

    def test_recovery_conflict_can_preserve_valid_external_revision_explicitly(self):
        self.create()
        old = self.vault.read("memory_one")
        with patch.object(self.vault, "_apply_change", side_effect=RuntimeError("power loss")):
            with self.assertRaises(RuntimeError):
                self.vault.commit("update", {"update": True}, lambda: (
                    [{"record": {**self.record(), "revision": 2}, "path": old["path"], "expected": old["etag"]}],
                    {"id": old["id"]},
                ))
        target = self.vault.home / old["path"]
        target.write_text(target.read_text("utf-8").replace("可复用的中文知识", "用户保留内容"), encoding="utf-8")
        pending = self.vault.recovery_status()["items"][0]
        expected = {item["path"]: item["current_etag"] for item in pending["files"]}
        with self.assertRaises(DiagnosticError):
            self.vault.resolve("update", {old["path"]: old["etag"]})
        resolved = self.vault.resolve("update", expected)
        self.assertTrue(resolved["abandoned"])
        self.assertEqual(self.vault.read("memory_one")["body"], "用户保留内容")
        self.assertEqual(self.vault.recovery_status()["items"], [])
        self.assertEqual(self.vault.resolve("update", expected), resolved)

    def test_rebuild_indexes_preserves_documents_and_operation_history(self):
        self.create()
        self.vault.rebuild()
        self.assertEqual(self.vault.read("memory_one")["body"], "可复用的中文知识")
        self.assertEqual(self.create(), {"id": "memory_one"})

    def test_delete_redacts_operation_content_and_tombstone_rejects_resurrection(self):
        self.create()
        old = self.vault.read("memory_one")
        self.vault.commit("forget", {"delete": old["id"]}, lambda: (
            [{"record": None, "path": old["path"], "expected": old["etag"], "id": old["id"]}],
            {"id": old["id"], "deleted": True},
        ))
        self.assertFalse((self.vault.home / old["path"]).exists())
        with self.assertRaises(DiagnosticError):
            self.vault.read(old["id"])
        self.create()
        self.assertFalse((self.vault.home / old["path"]).exists())
        with self.assertRaises(DiagnosticError) as caught:
            self.create(operation_id="new-create")
        self.assertEqual(caught.exception.code, "RECORD_DELETED")

    def test_write_cannot_escape_vault(self):
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.commit("escape", {"escape": True}, lambda: (
                [{"record": self.record(), "path": "../outside.md", "expected": None}], {"id": "x"}
            ))
        self.assertEqual(caught.exception.code, "SCOPE_DENIED")

    def test_invalid_external_metadata_and_directory_replacement_have_domain_errors(self):
        self.create()
        old = self.vault.read("memory_one")
        target = self.vault.home / old["path"]
        target.write_bytes(target.read_bytes().replace(b'"title":', b'"removed_title":'))
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.read("memory_one")
        self.assertEqual(caught.exception.code, "RECORD_INVALID")
        target.unlink()
        target.mkdir()
        with self.assertRaises(DiagnosticError):
            self.vault.read("memory_one")

    def test_same_identity_at_another_path_is_rejected(self):
        self.create()
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.commit("alias", {"alias": True}, lambda: (
                [{"record": self.record(), "path": "records/alias.md", "expected": None}], {"id": "memory_one"}
            ))
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        self.assertFalse((self.vault.home / "records/alias.md").exists())

    def test_external_edit_after_preflight_is_preserved(self):
        self.create()
        old = self.vault.read("memory_one")
        target = self.vault.home / old["path"]
        original_hash = self.vault._file_hash
        checks = 0
        def race(relative):
            nonlocal checks
            checks += 1
            if checks == 3:
                target.write_text("external content", encoding="utf-8")
            return original_hash(relative)
        with patch.object(self.vault, "_file_hash", side_effect=race):
            with self.assertRaises(DiagnosticError):
                self.vault.commit("race", {"update": True}, lambda: (
                    [{"record": {**self.record(), "revision": 2}, "path": old["path"], "expected": old["etag"]}],
                    {"id": old["id"]},
                ))
        self.assertEqual(target.read_text("utf-8"), "external content")

    def test_two_real_writers_with_same_etag_cannot_overwrite_each_other(self):
        self.create()
        old = self.vault.read("memory_one")
        code = """
import json,sys
from pathlib import Path
from memweave.storage import Vault
from memweave.diagnostics import DiagnosticError
vault=Vault(Path(sys.argv[1]))
old=json.loads(sys.argv[2])
try:
    answer=vault.commit(sys.argv[3], {"writer":sys.argv[3]}, lambda: (
        [{"record":{**old,"revision":2,"body":sys.argv[3]},"path":old["path"],"expected":old["etag"]}],
        {"id":old["id"]}))
    print("ok")
except DiagnosticError as error:
    print(error.code)
"""
        processes = [
            subprocess.Popen([sys.executable, "-c", code, str(self.vault.home),
                              json.dumps(old), writer], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
            for writer in ("writer-a", "writer-b")
        ]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            outcomes.append(stdout.strip())
        self.assertCountEqual(outcomes, ["ok", "VERSION_CONFLICT"])
        self.assertIn(self.vault.read("memory_one")["body"], ("writer-a", "writer-b"))

    def test_evidence_normalization_validates_kind_reference_and_limits(self):
        normalized = normalize_evidence([
            {
                "kind": " user ", "reference": " 用户确认 ", "hash": " abc ",
                "observed_at": " 2026-11-08 ", "summary": " 摘要 ", "unknown": "dropped",
            },
            {"kind": "test", "reference": "tests.test_storage"},
        ])
        self.assertEqual(normalized, [
            {
                "kind": "user", "reference": "用户确认", "hash": "abc",
                "observed_at": "2026-11-08", "summary": "摘要",
            },
            {"kind": "test", "reference": "tests.test_storage"},
        ])
        self.assertEqual(normalize_evidence([]), [])
        too_large = [
            {
                "kind": "file", "reference": "x" * 500, "hash": "h" * 128,
                "observed_at": "o" * 64, "summary": "s" * 500,
            }
            for _ in range(MAX_EVIDENCE_ITEMS)
        ]
        invalid_values = [
            "not-a-list",
            [1],
            [{"kind": "git", "reference": "HEAD"}],
            [{"kind": "file"}],
            [{"kind": "file", "reference": " "}],
            [{"kind": "file", "reference": "x" * 501}],
            [{"kind": "file", "reference": "x", "hash": "h" * 129}],
            [{"kind": "file", "reference": "x"} for _ in range(MAX_EVIDENCE_ITEMS + 1)],
            too_large,
        ]
        for value in invalid_values:
            with self.subTest(value=str(value)[:48]), self.assertRaises(DiagnosticError) as caught:
                normalize_evidence(value)
            self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def test_evidence_round_trips_in_markdown_and_survives_rebuild_without_duplication(self):
        evidence = sample_evidence()
        self.create({**self.record(), "evidence": evidence}, operation_id="evidence-create")
        restored = Vault(self.vault.home).read("memory_one")
        self.assertEqual(restored["evidence"], evidence)
        markdown = (self.vault.home / restored["path"]).read_text("utf-8")
        self.assertIn('"evidence"', markdown)
        self.vault.rebuild()
        rebuilt = self.vault.read("memory_one")
        self.assertEqual(rebuilt["evidence"], evidence)
        self.assertEqual(len(rebuilt["evidence"]), len(evidence))

    def test_legacy_records_without_evidence_read_as_empty(self):
        self.create()
        legacy_memory = Vault(self.vault.home).read("memory_one")
        self.assertNotIn("evidence", legacy_memory)
        self.assertEqual(record_evidence(legacy_memory), [])
        task = {
            "id": "task_legacy", "type": "task", "scope": "project",
            "project_id": "project_one", "title": "旧任务", "body": "",
            "revision": 1, "status": "current", "goal": "目标", "acceptance": ["验收"],
        }
        self.vault.commit("legacy-task", {"action": "legacy-task"}, lambda: (
            [{"record": task, "path": "tasks/task_legacy.md", "expected": None}],
            {"id": "task_legacy"},
        ))
        self.assertEqual(record_evidence(Vault(self.vault.home).read("task_legacy")), [])

    def test_malformed_evidence_in_markdown_is_rejected(self):
        self.create({**self.record(), "evidence": sample_evidence()})
        old = self.vault.read("memory_one")
        target = self.vault.home / old["path"]
        target.write_text(
            target.read_text("utf-8").replace('"kind": "file"', '"kind": "git"'), "utf-8"
        )
        with self.assertRaises(DiagnosticError) as caught:
            self.vault.read("memory_one")
        self.assertEqual(caught.exception.code, "RECORD_INVALID")

    def test_abrupt_process_exit_after_prepare_recovers_on_restart(self):
        code = """
import os,sys
from pathlib import Path
from memweave.storage import Vault
vault=Vault(Path(sys.argv[1]))
def interrupted(change):
    os._exit(73)
vault._apply_change=interrupted
record={"id":"crashed","type":"memory","scope":"user","title":"中断","body":"真实进程中断","revision":1}
vault.commit("crash",{"action":"crash"},lambda:([{"record":record,"path":"records/crashed.md","expected":None}],{"id":"crashed"}))
"""
        completed = subprocess.run([sys.executable, "-c", code, str(self.vault.home)],
                                   capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 73, completed.stderr)
        restarted = Vault(self.vault.home)
        self.assertEqual(restarted.recover()["recovered"], 1)
        self.assertEqual(restarted.read("crashed")["body"], "真实进程中断")


if __name__ == "__main__":
    unittest.main()
