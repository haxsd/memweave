import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from memweave.diagnostics import Diagnostics, DiagnosticError


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "记忆 库"
        self.service = Diagnostics(self.home)

    def test_status_does_not_create_an_uninitialized_home(self):
        status = self.service.status()
        self.assertFalse(status["data"]["initialized"])
        self.assertFalse(self.home.exists())

    def test_initialize_preserves_existing_probe_on_repeated_calls(self):
        self.service.initialize()
        self.service.write_probe("op-1", "从 Cursor 接续")
        self.service.initialize()
        restored = Diagnostics(self.home).read_probe("op-1")
        self.assertEqual(restored["data"]["marker"], "从 Cursor 接续")
        self.assertTrue(self.service.status()["data"]["initialized"])

    def test_read_before_init_does_not_create_a_database(self):
        with self.assertRaises(DiagnosticError) as caught:
            self.service.read_probe("op-1")
        self.assertEqual(caught.exception.code, "NOT_INITIALIZED")
        self.assertFalse(self.home.exists())

    def test_probe_retries_are_idempotent_and_content_conflicts_are_rejected(self):
        self.service.initialize()
        first = self.service.write_probe("op-1", "记忆")
        repeated = self.service.write_probe("op-1", "记忆")
        self.assertEqual(first, repeated)
        with self.assertRaises(DiagnosticError) as caught:
            self.service.write_probe("op-1", "被覆盖")
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        self.assertEqual(self.service.read_probe("op-1")["data"]["marker"], "记忆")

    def test_concurrent_writers_cannot_overwrite_same_operation(self):
        self.service.initialize()

        def attempt(marker):
            try:
                return Diagnostics(self.home).write_probe("shared", marker)["data"]["marker"]
            except DiagnosticError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ["first", "second"]))
        self.assertEqual(results.count("VERSION_CONFLICT"), 1)
        saved = self.service.read_probe("shared")["data"]["marker"]
        self.assertIn(saved, ["first", "second"])
        self.assertIn(saved, results)

    def test_invalid_probe_fields_are_rejected_before_writing(self):
        self.service.initialize()
        for operation_id, marker in [("", "valid"), ("op", ""), ("op", "x" * 1025)]:
            with self.subTest(operation_id=operation_id, length=len(marker)):
                with self.assertRaises(DiagnosticError) as caught:
                    self.service.write_probe(operation_id, marker)
                self.assertEqual(caught.exception.code, "RECORD_INVALID")
        self.assertEqual(self.service.status()["data"]["probe_count"], 0)

    def test_missing_probe_reports_actionable_error(self):
        self.service.initialize()
        with self.assertRaises(DiagnosticError) as caught:
            self.service.read_probe("missing")
        self.assertEqual(caught.exception.code, "PROBE_NOT_FOUND")

    def test_hook_receipts_keep_only_allowlisted_metadata_and_deduplicate(self):
        self.service.initialize()
        payload = {
            "conversation_id": "session-1",
            "generation_id": "generation-1",
            "prompt": "不要存储这个秘密",
            "transcript": ["private"],
        }
        self.service.record_hook("cursor", "sessionStart", payload)
        self.service.record_hook("cursor", "sessionStart", payload)
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            receipts = connection.execute("SELECT metadata FROM hook_receipts").fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0][0]),
            {"conversation_id": "session-1", "generation_id": "generation-1"},
        )
        status = self.service.status()["data"]
        self.assertEqual(status["hook_receipt_count"], 1)
        self.assertEqual(status["clients"]["cursor"]["desktop_verification"], "unverified")

    def test_empty_optional_hook_identifiers_are_omitted(self):
        self.service.initialize()
        self.service.record_hook(
            "cursor",
            "sessionStart",
            {"conversation_id": "session-1", "generation_id": "", "session_id": "session-1"},
        )
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            metadata = json.loads(
                connection.execute("SELECT metadata FROM hook_receipts").fetchone()[0]
            )
        self.assertEqual(metadata, {"conversation_id": "session-1", "session_id": "session-1"})

    def test_unknown_hook_event_cannot_be_silently_recorded(self):
        self.service.initialize()
        with self.assertRaises(DiagnosticError) as caught:
            self.service.record_hook("cursor", "notAnEvent", {})
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(self.service.status()["data"]["hook_receipt_count"], 0)

    def test_newer_schema_is_not_modified_by_initialization(self):
        self.service.initialize()
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(DiagnosticError) as caught:
            self.service.initialize()
        self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        with closing(sqlite3.connect(self.home / "index" / "memory.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_invalid_config_encoding_reports_a_config_error(self):
        self.service.initialize()
        self.service.config.write_bytes(b"\xff\xfe")
        with self.assertRaises(DiagnosticError) as caught:
            self.service.status()
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
