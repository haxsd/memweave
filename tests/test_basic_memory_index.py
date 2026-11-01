import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from memweave.diagnostics import Diagnostics
from memweave.services import Service


class BasicMemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "home"
        Diagnostics(self.home).initialize()
        self.service = Service(self.home)
        self.service.start()
        self.addCleanup(self.service.close)
        self.project = self.service.project_register(
            str(self.root), "index test", "project"
        )

    def test_search_index_rebuilds_from_unchanged_markdown(self):
        memory = self.service.memory_write(
            "Continuity boundary",
            "The SQLite index is derived from inspectable Markdown.",
            [{"type": "host", "reference": "test"}],
            "memory",
            project_id=self.project["id"],
        )
        record = self.service.memory_read(memory["id"])
        markdown_path = self.home / record["path"]
        original = markdown_path.read_bytes()
        self.assertTrue((self.home / "index" / "basic-memory" / "memory.db").is_file())
        self.assertEqual(
            [item["id"] for item in self.service.memory_search(
                self.project["id"], "derived"
            )["items"]],
            [memory["id"]],
        )

        self.service.close()
        shutil.rmtree(self.home / "index" / "basic-memory")
        self.assertFalse((self.home / "index" / "basic-memory").exists())

        with Service(self.home) as restarted:
            hits = restarted.memory_search(self.project["id"], "inspectable")
            self.assertEqual([item["id"] for item in hits["items"]], [memory["id"]])
            self.assertEqual(hits["coverage"]["strategy"], "basic_memory_sqlite_fts5")
        self.assertEqual(markdown_path.read_bytes(), original)

    def test_rebuild_keeps_index_available_while_another_connection_is_open(self):
        memory = self.service.memory_write(
            "Live rebuild boundary",
            "A rebuild must not unlink a SQLite file that another runtime holds open.",
            [{"type": "host", "reference": "test"}],
            "memory",
            project_id=self.project["id"],
        )
        database = self.home / "index" / "basic-memory" / "memory.db"
        other_connection = sqlite3.connect(database)
        self.addCleanup(other_connection.close)
        self.assertEqual(
            other_connection.execute("SELECT COUNT(*) FROM project").fetchone()[0],
            1,
        )

        rebuilt = self.service.rebuild()

        self.assertEqual(rebuilt["search_index"]["failed"], [])
        self.assertTrue(self.service.index.active)
        self.assertTrue(database.is_file())
        hits = self.service.memory_search(
            self.project["id"], "SQLite file another runtime"
        )["items"]
        self.assertEqual([item["id"] for item in hits], [memory["id"]])


if __name__ == "__main__":
    unittest.main()
