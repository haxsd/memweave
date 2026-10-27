import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from memweave.diagnostics import Diagnostics
from memweave.integrations import server_command


class McpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "织忆 ' MCP home"
        Diagnostics(self.home).initialize()

    def client(self, mode="auto"):
        return Client(
            StdioServerParameters(**server_command(self.home, toolset="full"), cwd=self.temporary.name),
            mode=mode,
            read_timeout_seconds=10,
        )

    async def test_marker_survives_real_stdio_process_restart(self):
        async with asyncio.timeout(30):
            async with self.client() as client:
                tools = (await client.list_tools()).tools
                names = {tool.name for tool in tools}
                self.assertTrue({
                    "memweave_system_status", "memweave_probe_write", "memweave_probe_read",
                    "memweave_task_open", "memweave_task_read", "memweave_memory_search",
                    "memweave_graph_query", "memweave_context_get",
                    "memweave_workspace_prepare",
                }.issubset(names))
                readonly = next(tool for tool in tools if tool.name == "memweave_probe_read")
                self.assertTrue(readonly.annotations.model_dump(by_alias=True)["readOnlyHint"])
                written = await client.call_tool(
                    "memweave_probe_write", {"operation_id": "switch-1", "marker": "交接给下一工具"}
                )
                self.assertFalse(written.is_error)
                self.assertEqual(written.structured_content["data"]["marker"], "交接给下一工具")
            async with self.client() as client:
                restored = await client.call_tool(
                    "memweave_probe_read", {"operation_id": "switch-1"}
                )
                self.assertFalse(restored.is_error)
                self.assertEqual(restored.structured_content["data"]["marker"], "交接给下一工具")
                status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                self.assertEqual(status["probe_count"], 1)
                self.assertEqual(status["clients"]["codex"]["desktop_verification"], "unverified")

    async def test_business_and_parameter_errors_are_mcp_tool_errors(self):
        async with asyncio.timeout(20):
            async with self.client() as client:
                first = await client.call_tool(
                    "memweave_probe_write", {"operation_id": "retry", "marker": "original"}
                )
                repeated = await client.call_tool(
                    "memweave_probe_write", {"operation_id": "retry", "marker": "original"}
                )
                self.assertEqual(first.structured_content, repeated.structured_content)
                conflicting = await client.call_tool(
                    "memweave_probe_write", {"operation_id": "retry", "marker": "overwrite"}
                )
                self.assertTrue(conflicting.is_error)
                self.assertIn("VERSION_CONFLICT", conflicting.content[0].text)
                missing = await client.call_tool(
                    "memweave_probe_read", {"operation_id": "missing"}
                )
                self.assertTrue(missing.is_error)
                self.assertIn("PROBE_NOT_FOUND", missing.content[0].text)
                invalid = await client.call_tool(
                    "memweave_probe_write", {"operation_id": "new", "marker": "x" * 1025}
                )
                self.assertTrue(invalid.is_error)
                status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                self.assertEqual(status["probe_count"], 1)

    async def test_task_and_memory_survive_host_process_switch(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]
                project = await call("project_register", root=self.temporary.name, name="切换演示", operation_id="p")
                task = await call("task_open", project_id=project["id"], title="交接", goal="跨工具接续",
                                  acceptance=["保留下一步"], operation_id="t")
                latest = await call("task_read", task_id=task["id"])
                await call("task_checkpoint", task_id=task["id"], expected_revision=latest["revision"],
                           expected_etag=latest["etag"], findings="已定位", progress="实现中",
                           next_step="运行验证", operation_id="cp")
            async with self.client() as client:
                task_response = await client.call_tool("memweave_task_read", {"task_id": task["id"]})
                self.assertEqual(task_response.structured_content["data"]["next_step"], "运行验证")
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "resume", "query": "交接", "budget": 2000,
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
                self.assertEqual(prepared.structured_content["data"]["task_id"], task["id"])
                self.assertIn("运行验证", prepared.structured_content["data"]["context"]["text"])
                self.assertEqual(prepared.structured_content["data"]["context"]["graph"]["coverage"]["state"],
                                 "not_scanned")
                written = await client.call_tool("memweave_memory_write", {
                    "project_id": project["id"], "title": "交接规则", "body": "保存下一步再切换工具",
                    "sources": [{"type": "task", "reference": task["id"]}], "operation_id": "m",
                    "status": "candidate",
                })
                self.assertFalse(written.is_error, str(written.content))
                hidden = await client.call_tool("memweave_memory_search", {"project_id": project["id"], "query": "交接"})
                self.assertEqual(hidden.structured_content["data"]["items"], [])
                candidate = written.structured_content["data"]
                activated = await client.call_tool("memweave_memory_activate", {
                    "memory_id": candidate["id"], "expected_revision": candidate["revision"],
                    "expected_etag": candidate["etag"], "operation_id": "activate",
                })
                self.assertFalse(activated.is_error, str(activated.content))
                restored = await client.call_tool("memweave_memory_search", {
                    "project_id": project["id"], "query": "交接",
                })
                self.assertEqual(len(restored.structured_content["data"]["items"]), 1)
                await client.call_tool("memweave_memory_update", {
                    "memory_id": candidate["id"], "expected_revision": activated.structured_content["data"]["revision"],
                    "expected_etag": activated.structured_content["data"]["etag"], "title": "交接规则",
                    "body": "纠正后的交接经验", "operation_id": "correct",
                })
                reused = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "new", "query": "交接",
                })
                self.assertFalse(reused.is_error, str(reused.content))
                self.assertIn("纠正后的交接经验", reused.structured_content["data"]["context"]["text"])
                self.assertIsNone(reused.structured_content["data"]["task_id"])
                self.assertNotIn("运行验证", reused.structured_content["data"]["context"]["text"])

    async def test_legacy_protocol_clients_can_call_the_same_tools(self):
        async with asyncio.timeout(20):
            async with self.client(mode="legacy") as client:
                status = await client.call_tool("memweave_system_status")
                self.assertFalse(status.is_error)
                self.assertTrue(status.structured_content["data"]["initialized"])

    async def test_probe_cli_reports_sdk_evidence_without_claiming_desktop_verification(self):
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "memweave", "probe", "--home", str(self.home)],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=40,
            cwd=self.temporary.name,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = json.loads(completed.stdout)["data"]
        self.assertEqual(evidence["verification_source"], "sdk_stdio")
        self.assertTrue(evidence["restart_readback"])
        self.assertEqual(evidence["desktop_verification"], "unverified")
        self.assertTrue(evidence["protocol_version"])


if __name__ == "__main__":
    unittest.main()
