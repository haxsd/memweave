import asyncio
import tempfile
import unittest
from pathlib import Path

from memweave.demo import run_demo
from memweave.services import Service


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_uses_two_servers_and_leaves_readable_completed_task_and_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "示例项目"
            root.mkdir()
            (root / "README.md").write_text("# Demo\n\n本地演示项目", encoding="utf-8")
            home = Path(temporary) / "记忆库"
            async with asyncio.timeout(40):
                output = await run_demo(home, root)
            self.assertTrue(output["data"]["restart_readback"])
            self.assertTrue(output["data"]["forgotten_verified"])
            with Service(home) as service:
                task = service.task_read(output["data"]["task_id"])
                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["verification"], "host_reported")
                self.assertEqual(len(service.memory_search(output["data"]["project_id"], "接续")["items"]), 1)
            self.assertIn("演示", service.vault.read(output["data"]["architecture_id"])["title"])


if __name__ == "__main__":
    unittest.main()
