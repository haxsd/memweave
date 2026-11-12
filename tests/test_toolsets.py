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
    async def test_one_universal_catalog_and_legacy_flag_does_not_change_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            Service(home).initialize()
            async with asyncio.timeout(30):
                async with Client(StdioServerParameters(**server_command(home))) as client:
                    universal = (await client.list_tools()).tools
                    names = {tool.name for tool in universal}
                    self.assertEqual(len(names), 22)
                    self.assertIn("memweave_workspace_prepare", names)
                    self.assertIn("memweave_task_list", names)
                    self.assertNotIn("memweave_graph_forget", names)
                    self.assertNotIn("memweave_memory_read", names)
                    self.assertNotIn("memweave_context_get", names)
                    self.assertNotIn("memweave_probe_write", names)
                    rejected = await client.call_tool("memweave_recover", {})
                    self.assertTrue(rejected.is_error)
                    status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                    self.assertEqual(status["toolset"], "universal")
                    self.assertEqual(status["tool_count"], 22)

                configuration = server_command(home)
                configuration["args"] += ["--toolset", "full"]
                async with Client(StdioServerParameters(**configuration)) as client:
                    legacy = (await client.list_tools()).tools
                    self.assertEqual({tool.name for tool in legacy}, names)
                    status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                    self.assertEqual(status["tool_count"], 22)

                diagnostic = server_command(home, diagnostic=True)
                async with Client(StdioServerParameters(**diagnostic)) as client:
                    probe_tools = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertIn("memweave_probe_write", probe_tools)
                    self.assertIn("memweave_probe_read", probe_tools)
                    self.assertIn("memweave_context_get", probe_tools)
                    self.assertEqual(len(probe_tools), 31)
