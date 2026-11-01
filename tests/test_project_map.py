import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memweave.diagnostics import DiagnosticError
from memweave.project_map import ProjectMap
from memweave.storage import Vault


class ProjectMapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.vault = Vault(self.root / "memory-home")
        self.vault.initialize()
        project = {
            "id": "project_one", "type": "project", "scope": "project",
            "project_id": "project_one", "root": str(self.root), "revision": 1,
            "title": "Mixed project", "body": "",
        }
        self.vault.commit("register", project, lambda: (
            [{"record": project, "path": "projects/project_one.md", "expected": None}],
            {"id": project["id"]},
        ))
        self.maps = ProjectMap(self.vault)

    def file(self, relative, body="hello"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def error(self, code, callback):
        with self.assertRaises(DiagnosticError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_scan_builds_language_independent_structure_without_memory_home(self):
        self.file("src/应用.py", "print('织忆')")
        self.file("src/App.java", "class App {}")
        scanned = self.maps.scan("project_one", "scan-one")
        graph = self.maps.query("project_one")
        paths = {node["path"] for node in graph["nodes"]}
        self.assertTrue({"src", "src/应用.py", "src/App.java"}.issubset(paths))
        self.assertFalse(any("memory-home" in path for path in paths))
        self.assertEqual(scanned["file_count"], 2)
        self.assertTrue(any(edge["relation"] == "contains" for edge in graph["edges"]))
        self.assertEqual(graph["coverage"]["generation"], 1)

    def test_scan_obeys_nested_gitignore_negation_and_sensitive_exclusions(self):
        self.file(".gitignore", "*.log\n")
        self.file("src/.gitignore", "*.tmp\n!keep.tmp\n")
        for name in ("root.log", "src/debug.log", "src/drop.tmp", ".env",
                     "secrets.json", "cert.key", "node_modules/lib.js", "dist/app.js"):
            self.file(name)
        self.file("src/keep.tmp")
        self.file("src/app.py")
        self.maps.scan("project_one", "scan")
        paths = {node["path"] for node in self.maps.query("project_one")["nodes"]}
        self.assertIn("src/keep.tmp", paths)
        self.assertIn("src/app.py", paths)
        self.assertTrue({"root.log", "src/debug.log", "src/drop.tmp", ".env",
                         "secrets.json", "cert.key", "node_modules/lib.js",
                         "dist/app.js"}.isdisjoint(paths))

    def test_scan_operation_is_idempotent_and_content_changes_create_generation(self):
        target = self.file("main.py", "first")
        first = self.maps.scan("project_one", "scan-one")
        target.write_text("other", encoding="utf-8")
        self.assertEqual(self.maps.scan("project_one", "scan-one"), first)
        second = self.maps.scan("project_one", "scan-two")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second["changed"], ["main.py"])
        self.assertEqual(second["file_count"], 1)
        target.unlink()
        third = self.maps.scan("project_one", "scan-three")
        self.assertEqual(third["deleted"], ["main.py"])

    def test_evidence_requires_scanned_current_source_and_enforces_budget(self):
        target = self.file("main.py", "abcdef织忆")
        self.maps.scan("project_one", "scan")
        evidence = self.maps.evidence("project_one", "main.py", max_chars=4)
        self.assertEqual(evidence["content"], "abcd")
        self.assertTrue(evidence["truncated"])
        self.assertEqual(evidence["hash"], hashlib.sha256(target.read_bytes()).hexdigest())
        target.write_text("changed", encoding="utf-8")
        self.error("SOURCE_CHANGED", lambda: self.maps.evidence("project_one", "main.py"))
        self.error("SCOPE_DENIED", lambda: self.maps.evidence("project_one", "../outside"))
        self.error("SCOPE_DENIED", lambda: self.maps.evidence("project_one", str(target)))
        self.error("SOURCE_NOT_INDEXED", lambda: self.maps.evidence("project_one", ".env"))

    def test_symlink_directory_and_file_are_not_scanned_or_read(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private", encoding="utf-8")
        try:
            os.symlink(outside, self.root / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("Windows account cannot create symlinks")
        self.file("safe.py")
        self.maps.scan("project_one", "scan")
        paths = {node["path"] for node in self.maps.query("project_one")["nodes"]}
        self.assertNotIn("linked/private.txt", paths)
        self.error("SCOPE_DENIED", lambda: self.maps.evidence("project_one", "linked/private.txt"))

    def test_bounds_report_partial_coverage_and_exclude_binary(self):
        self.file("a.txt", "12345")
        self.file("b.txt", "67890")
        self.file("c.txt", "\x00binary")
        with patch("memweave.project_map.MAX_SCAN_FILES", 1):
            scanned = self.maps.scan("project_one", "scan")
        self.assertEqual(scanned["file_count"], 1)
        self.assertEqual(scanned["coverage"]["state"], "partial")
        self.assertTrue(scanned["coverage"]["truncated"])
        self.assertTrue(scanned["skipped"])

    def test_size_and_total_byte_limits_are_reported(self):
        self.file("a.txt", "12345")
        self.file("b.txt", "12345")
        with patch("memweave.project_map.MAX_FILE_BYTES", 4):
            scanned = self.maps.scan("project_one", "size")
        self.assertEqual(scanned["file_count"], 0)
        self.assertGreater(scanned["skipped"], 0)
        with patch("memweave.project_map.MAX_SCAN_BYTES", 6):
            scanned = self.maps.scan("project_one", "total")
        self.assertEqual(scanned["file_count"], 1)
        self.assertTrue(scanned["coverage"]["truncated"])

    def test_architecture_is_durable_idempotent_and_rejects_unverified_sources(self):
        self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        sources = [{"path": source["path"], "hash": source["hash"]}]
        first = self.maps.write("project_one", "write", "入口", "启动系统", sources, [])
        self.assertEqual(self.maps.write("project_one", "write", "入口", "启动系统", sources, []), first)
        record = self.vault.read(first["id"])
        self.assertEqual(record["type"], "architecture")
        self.assertEqual(record["scope"], "project")
        self.assertEqual(record["sources"], sources)
        self.assertEqual(record["etag"], first["etag"])
        self.error("SOURCE_CHANGED", lambda: self.maps.write(
            "project_one", "bad-source", "Bad", "Body",
            [{"path": "main.py", "hash": "0" * 64}], [],
        ))
        self.error("RECORD_INVALID", lambda: self.maps.write(
            "project_one", "no-source", "Bad", "Body", [], [],
        ))

    def test_source_drift_excludes_architecture_before_and_after_rescan(self):
        target = self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        record = self.maps.write("project_one", "write", "Entry", "Main", [
            {"path": source["path"], "hash": source["hash"]},
        ], [{"source": "entry", "target": "file:main.py", "relation": "configured_by"}])
        self.assertIn(record["id"], {node["id"] for node in self.maps.query("project_one")["nodes"]})
        target.write_text("changed", encoding="utf-8")
        self.assertNotIn(record["id"], {node["id"] for node in self.maps.query("project_one")["nodes"]})
        graph = self.maps.query("project_one", include_stale=True)
        node = next(node for node in graph["nodes"] if node["id"] == record["id"])
        self.assertEqual(node["status"], "needs_review")
        self.assertEqual(graph["freshness"]["state"], "stale")
        self.maps.scan("project_one", "rescan")
        self.assertNotIn(record["id"], {node["id"] for node in self.maps.query("project_one")["nodes"]})

    def test_architecture_update_exposes_version_and_forget_uses_tombstone(self):
        self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        created = self.maps.write("project_one", "write", "入口", "启动系统", [source], [])
        graph = self.maps.query("project_one")
        node = next(node for node in graph["nodes"] if node["id"] == created["id"])
        self.assertEqual(node["revision"], 1)
        self.assertEqual(node["etag"], created["etag"])
        self.assertEqual(node["stale_reasons"], [])

        updated = self.maps.update(
            "project_one", "update", created["id"], 1, created["etag"],
            "新入口", "由 app 启动", [source], [
                {"source": "entry", "target": "file:main.py", "relation": "starts"},
            ],
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["status"], "active")
        node = next(
            node for node in self.maps.query("project_one")["nodes"]
            if node["id"] == created["id"]
        )
        self.assertEqual(node["revision"], 2)
        self.assertEqual(node["etag"], updated["etag"])
        self.assertEqual(node["title"], "新入口")
        self.error("VERSION_CONFLICT", lambda: self.maps.update(
            "project_one", "stale-update", created["id"], 1, created["etag"],
            "旧写", "不应覆盖", [source], [],
        ))

        forgotten = self.maps.forget(
            "project_one", "forget", created["id"], 2, updated["etag"],
        )
        self.assertTrue(forgotten["deleted"])
        self.error("RECORD_NOT_FOUND", lambda: self.vault.read(created["id"]))
        self.error("RECORD_NOT_FOUND", lambda: self.maps.forget(
            "project_one", "forget-again", created["id"], 2, updated["etag"],
        ))
        self.assertNotIn(
            created["id"],
            {node["id"] for node in self.maps.query("project_one", include_stale=True)["nodes"]},
        )
        resurrected = {
            "id": created["id"], "type": "architecture", "scope": "project",
            "project_id": "project_one", "title": "Resurrected", "body": "",
            "sources": [source], "relations": [], "status": "active",
            "revision": 1, "created_at": "2026-10-28T00:00:00+00:00",
            "source_type": "host_reported",
        }
        self.error("RECORD_DELETED", lambda: self.vault.commit(
            "resurrect", {"id": created["id"]},
            lambda: ([{"record": resurrected, "path": "architecture/old.md", "expected": None}], {}),
        ))

    def test_architecture_update_rejects_unscanned_or_changed_sources(self):
        target = self.file("main.py", "first")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        created = self.maps.write("project_one", "write", "Entry", "Main", [source], [])
        target.write_text("changed", encoding="utf-8")
        self.error("SOURCE_CHANGED", lambda: self.maps.update(
            "project_one", "update-stale", created["id"], 1, created["etag"],
            "Entry", "Main changed", [source], [],
        ))
        rescan = self.maps.scan("project_one", "rescan")
        self.assertEqual(rescan["changed"], ["main.py"])
        current = self.maps.evidence("project_one", "main.py")
        updated = self.maps.update(
            "project_one", "update-current", created["id"], 1, created["etag"],
            "Entry", "Main changed", [current], [],
        )
        self.assertEqual(updated["revision"], 2)
        graph = self.maps.query("project_one", include_stale=True)
        node = next(node for node in graph["nodes"] if node["id"] == created["id"])
        self.assertEqual(node["status"], "active")
        self.assertEqual(node["stale_reasons"], [])

    def test_architecture_update_is_project_scoped(self):
        self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        created = self.maps.write("project_one", "write", "Entry", "Main", [source], [])
        other_root = Path(self.temporary.name) / "other"
        other_root.mkdir()
        other = {
            "id": "project_two", "type": "project", "scope": "project",
            "project_id": "project_two", "root": str(other_root), "revision": 1,
            "title": "Other project", "body": "",
        }
        self.vault.commit("register-two", other, lambda: (
            [{"record": other, "path": "projects/project_two.md", "expected": None}],
            {"id": other["id"]},
        ))
        self.error("SCOPE_DENIED", lambda: self.maps.update(
            "project_two", "cross-project", created["id"], 1, created["etag"],
            "Wrong", "Wrong", [source], [],
        ))
        self.error("SCOPE_DENIED", lambda: self.maps.forget(
            "project_two", "cross-project-forget", created["id"], 1, created["etag"],
        ))

    def test_query_filters_and_limits_nodes_and_edges(self):
        for number in range(4):
            self.file(f"src/file{number}.py")
        self.maps.scan("project_one", "scan")
        graph = self.maps.query("project_one", query="file2")
        self.assertTrue(any(node["path"] == "src/file2.py" for node in graph["nodes"]))
        self.assertFalse(any(node["path"] == "src/file1.py" for node in graph["nodes"]))
        limited = self.maps.query("project_one", limit=2)
        self.assertLessEqual(len(limited["nodes"]), 2)
        self.assertTrue(limited["coverage"]["truncated"])
        ids = {node["id"] for node in limited["nodes"]}
        self.assertTrue(all(edge["source"] in ids and edge["target"] in ids for edge in limited["edges"]))

    def test_query_without_scan_reports_unknown_coverage(self):
        graph = self.maps.query("project_one")
        self.assertEqual(graph["coverage"]["state"], "not_scanned")
        self.assertEqual(graph["freshness"]["state"], "unknown")

    def test_validated_by_requires_explicit_evidence_kind(self):
        self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        self.error("RECORD_INVALID", lambda: self.maps.write(
            "project_one", "write", "Main", "Body", [source],
            [{"source": "main", "target": "test", "relation": "validated_by"}],
        ))

    def test_junction_directory_is_excluded_without_symlink_privileges(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        import _winapi
        outside = Path(self.temporary.name) / "private"
        outside.mkdir()
        (outside / "private.txt").write_text("private", encoding="utf-8")
        _winapi.CreateJunction(str(outside), str(self.root / "junction"))
        self.addCleanup((self.root / "junction").rmdir)
        self.maps.scan("project_one", "scan")
        paths = {node["path"] for node in self.maps.query("project_one")["nodes"]}
        self.assertNotIn("junction/private.txt", paths)
        self.error("SCOPE_DENIED", lambda: self.maps.evidence("project_one", "junction/private.txt"))

    def test_binary_and_singular_secret_names_are_excluded(self):
        self.file("binary.dat", "\x00binary")
        self.file("secret.yaml", "token: private")
        self.file("source.py", "hello")
        self.maps.scan("project_one", "scan")
        paths = {node["path"] for node in self.maps.query("project_one")["nodes"]}
        self.assertNotIn("binary.dat", paths)
        self.assertNotIn("secret.yaml", paths)
        self.assertIn("source.py", paths)

    def test_oversized_ignore_file_does_not_expose_unprocessed_directory(self):
        self.file("src/.gitignore", "private.txt\n" * 20)
        self.file("src/private.txt", "secret")
        with patch("memweave.project_map.MAX_FILE_BYTES", 100):
            scan = self.maps.scan("project_one", "scan")
        paths = {node["path"] for node in self.maps.query("project_one")["nodes"]}
        self.assertNotIn("src/private.txt", paths)
        self.assertEqual(scan["coverage"]["state"], "partial")

    def test_edge_budget_reports_truncation(self):
        self.file("main.py")
        self.maps.scan("project_one", "scan")
        source = self.maps.evidence("project_one", "main.py")
        self.maps.write("project_one", "write", "Repeated", "Body", [source], [
            {"source": "self", "target": "self", "relation": "related_to"},
        ] * 3)
        graph = self.maps.query("project_one", query="Repeated", limit=1)
        self.assertEqual(len(graph["edges"]), 2)
        self.assertTrue(graph["coverage"]["truncated"])


if __name__ == "__main__":
    unittest.main()
