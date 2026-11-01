import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memweave.basic_memory_engine import BasicMemoryEngine


class BasicMemoryEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name) / "project"
        self.engine = BasicMemoryEngine(root, Path(self.temp.name) / "index")
        await self.engine.start()

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_text_search_and_context_share_one_basic_memory_index(self):
        note = await self.engine.write_note(
            "Unified memory boundary",
            "Basic Memory supplies the Markdown index and context pipeline.",
            directory="notes",
            note_type="decision",
        )
        self.assertTrue(note["created"])
        self.assertTrue((self.engine.vault / note["file_path"]).is_file())

        results = await self.engine.search("context pipeline")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["file_path"], note["file_path"])

        context = await self.engine.context(note["permalink"])
        self.assertGreaterEqual(len(context.results), 1)
        self.assertEqual(context.results[0].primary_result.file_path, note["file_path"])
