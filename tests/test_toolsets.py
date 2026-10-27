import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from memweave.integrations import server_command
from memweave.services import Service


class ToolsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_catalog_is_smaller_and_hidden_tools_cannot_be_called(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            Service(home).initialize()
            async with asyncio.timeout(30):
                async with Client(StdioServerParameters(**server_command(home))) as client:
                    core = (await client.list_tools()).tools
                    names = {tool.name for tool in core}
                    self.assertEqual(len(names), 20)
                    self.assertIn("memweave_workspace_prepare", names)
                    self.assertIn("memweave_task_list", names)
                    self.assertNotIn("memweave_context_get", names)
                    self.assertNotIn("memweave_probe_write", names)
                    rejected = await client.call_tool("memweave_recover", {})
                    self.assertTrue(rejected.is_error)
                    status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                    self.assertEqual(status["toolset"], "core")
                configuration = server_command(home)
                configuration["args"] += ["--toolset", "full"]
                async with Client(StdioServerParameters(**configuration)) as client:
                    full = (await client.list_tools()).tools
                    self.assertEqual(len(full), 27)
                    self.assertIn("memweave_context_get", {tool.name for tool in full})
                    status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                    self.assertEqual(status["tool_count"], 27)
                core_size = len(json.dumps([tool.model_dump(by_alias=True) for tool in core]))
                full_size = len(json.dumps([tool.model_dump(by_alias=True) for tool in full]))
                self.assertLess(core_size, full_size)
