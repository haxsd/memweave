import asyncio
import json
import os
import shutil
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
            StdioServerParameters(**server_command(self.home, diagnostic=True), cwd=self.temporary.name),
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
                    "memweave_work_unit_update", "memweave_task_phase_close",
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
                invalid_source = await client.call_tool("memweave_memory_write", {
                    "scope": "user", "title": "invalid source", "body": "must not write",
                    "sources": ["missing-separator"], "operation_id": "invalid-source",
                })
                self.assertTrue(invalid_source.is_error)
                self.assertIn("type:reference", invalid_source.content[0].text)
                status = (await client.call_tool("memweave_system_status")).structured_content["data"]
                self.assertEqual(status["probe_count"], 1)

    async def test_core_tool_arguments_have_no_nested_object_schema(self):
        def nested_object(schema):
            if not isinstance(schema, dict):
                return False
            if schema.get("type") == "object":
                return True
            if schema.get("type") == "array" and nested_object(schema.get("items")):
                return True
            return any(
                nested_object(item)
                for key in ("anyOf", "oneOf", "allOf")
                for item in schema.get(key, [])
            )

        configuration = server_command(self.home)
        async with asyncio.timeout(30):
            async with Client(
                StdioServerParameters(**configuration), read_timeout_seconds=10
            ) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual(len(tools), 22)
                violations = []
                for tool in tools:
                    schema = tool.model_dump(by_alias=True).get("inputSchema", {})
                    for name, value in schema.get("properties", {}).items():
                        if nested_object(value):
                            violations.append(f"{tool.name}.{name}")
                self.assertEqual(violations, [])

    async def test_agent_memory_write_defaults_to_candidate_until_activation(self):
        async with asyncio.timeout(30):
            async with self.client() as client:
                project = await client.call_tool("memweave_project_register", {
                    "root": self.temporary.name, "name": "candidate default", "operation_id": "project",
                })
                self.assertFalse(project.is_error, str(project.content))
                project_id = project.structured_content["data"]["id"]
                written = await client.call_tool("memweave_memory_write", {
                    "project_id": project_id, "title": "phase note",
                    "body": "candidate default sentinel should stay hidden",
                    "sources": ["host:test"], "operation_id": "memory",
                })
                self.assertFalse(written.is_error, str(written.content))
                candidate = written.structured_content["data"]
                self.assertEqual(candidate["status"], "candidate")
                active = await client.call_tool("memweave_memory_search", {
                    "project_id": project_id, "query": "candidate default sentinel",
                })
                self.assertEqual(active.structured_content["data"]["items"], [])
                activated = await client.call_tool("memweave_memory_activate", {
                    "memory_id": candidate["id"], "expected_revision": candidate["revision"],
                    "expected_etag": candidate["etag"], "operation_id": "activate",
                })
                self.assertFalse(activated.is_error, str(activated.content))
                visible = await client.call_tool("memweave_memory_search", {
                    "project_id": project_id, "query": "candidate default sentinel",
                })
                self.assertEqual(
                    [item["id"] for item in visible.structured_content["data"]["items"]],
                    [candidate["id"]],
                )

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
                unit = await call(
                    "work_unit_update", task_id=task["id"], title="并行验证",
                    owner="worker-a", status="running", progress="正在验证",
                    next_step="汇报结果", operation_id="unit",
                )
                latest = await call("task_read", task_id=task["id"])
                await call("task_checkpoint", task_id=task["id"], expected_revision=latest["revision"],
                           expected_etag=latest["etag"], findings="已定位", progress="实现中",
                           next_step="运行验证", operation_id="cp")
            async with self.client() as client:
                task_response = await client.call_tool("memweave_task_read", {"task_id": task["id"]})
                self.assertEqual(task_response.structured_content["data"]["next_step"], "运行验证")
                self.assertEqual(
                    task_response.structured_content["data"]["work_units"][0]["id"], unit["id"]
                )
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "resume", "query": "交接", "budget": 2000,
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
                self.assertEqual(prepared.structured_content["data"]["task_id"], task["id"])
                self.assertIn("运行验证", prepared.structured_content["data"]["context"]["text"])
                self.assertEqual(prepared.structured_content["data"]["context"]["graph"]["coverage"]["state"],
                                 "not_scanned")
                self.assertIn("worker-a", prepared.structured_content["data"]["context"]["text"])
                written = await client.call_tool("memweave_memory_write", {
                    "project_id": project["id"], "title": "交接规则", "body": "保存下一步再切换工具",
                    "sources": [f"task:{task['id']}"], "operation_id": "m",
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

    async def test_queue_work_units_and_phase_over_real_stdio(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="stdio queue",
                    operation_id="project",
                )
                first = await call(
                    "task_open", project_id=project["id"], title="当前任务",
                    goal="验证队列", acceptance=["验收"], operation_id="first",
                )
                second = await call(
                    "task_open", project_id=project["id"], title="排队任务",
                    goal="等待推进", acceptance=["验收"], operation_id="second",
                )
                self.assertEqual(first["status"], "current")
                self.assertEqual(second["status"], "queued")
                unit = await call(
                    "work_unit_update", task_id=first["id"], title="并行单元",
                    owner="worker-stdio", status="running", progress="开始",
                    next_step="完成", operation_id="unit",
                )
                blocked = await client.call_tool("memweave_task_close", {
                    "task_id": first["id"], "expected_revision": first["revision"],
                    "expected_etag": first["etag"], "outcome": "completed",
                    "summary": "验收完成", "acceptance_results": ["通过"],
                    "operation_id": "premature-close",
                })
                self.assertTrue(blocked.is_error)
                self.assertIn("WORK_UNITS_ACTIVE", blocked.content[0].text)

                await call(
                    "work_unit_update", unit_id=unit["id"],
                    expected_revision=unit["revision"], expected_etag=unit["etag"],
                    status="completed", progress="完成", verification="stdio passed",
                    operation_id="unit-complete",
                )
                phase = await call(
                    "task_phase_close", task_id=first["id"],
                    expected_revision=first["revision"], expected_etag=first["etag"],
                    title="实现阶段", summary="实现完成", verification="stdio passed",
                    next_step="关闭任务", operation_id="phase",
                )
                detail = await call("task_read", task_id=first["id"])
                self.assertEqual(detail["status"], "current")
                self.assertEqual(detail["phases"][0]["id"], phase["phase_id"])
                closed = await call(
                    "task_close", task_id=first["id"], expected_revision=detail["revision"],
                    expected_etag=detail["etag"], outcome="completed", summary="任务完成",
                    acceptance_results=["stdio 通过"], operation_id="close",
                )
                self.assertEqual(closed["promoted_task_id"], second["id"])
                resumed = await call("workspace_prepare", root=self.temporary.name, intent="resume")
                self.assertEqual(resumed["task_id"], second["id"])

    async def test_two_stdio_processes_update_distinct_work_units(self):
        async with asyncio.timeout(60):
            async with self.client() as first_client, self.client() as second_client:
                async def call(client, action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    first_client, "project_register", root=self.temporary.name,
                    name="parallel stdio", operation_id="parallel-project",
                )
                task = await call(
                    first_client, "task_open", project_id=project["id"],
                    title="跨进程并行", goal="验证独立 WorkUnit",
                    acceptance=["两个单元互不覆盖"], operation_id="parallel-task",
                )
                units = [
                    await call(
                        first_client, "work_unit_update", task_id=task["id"],
                        title=f"并行单元 {index}", owner=f"worker-{index}",
                        status="running", progress="已分派",
                        operation_id=f"parallel-unit-{index}",
                    )
                    for index in (1, 2)
                ]

                async def complete(client, position):
                    unit = units[position]
                    return await call(
                        client, "work_unit_update", unit_id=unit["id"],
                        expected_revision=unit["revision"], expected_etag=unit["etag"],
                        status="completed", progress=f"worker-{position + 1} 完成",
                        verification=f"client-{position + 1}",
                        operation_id=f"parallel-complete-{position + 1}",
                    )

                completed = await asyncio.gather(
                    complete(first_client, 0),
                    complete(second_client, 1),
                )
                self.assertEqual({item["status"] for item in completed}, {"completed"})
                detail = await call(first_client, "task_read", task_id=task["id"])
                self.assertEqual(detail["revision"], 1)
                self.assertEqual(
                    {unit["status"] for unit in detail["work_units"]},
                    {"completed"},
                )
                self.assertEqual(
                    {unit["owner"] for unit in detail["work_units"]},
                    {"worker-1", "worker-2"},
                )

    async def test_one_process_rebuilds_search_from_markdown_after_restart(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                project = await client.call_tool("memweave_project_register", {
                    "root": self.temporary.name, "name": "rebuild", "operation_id": "project",
                })
                self.assertFalse(project.is_error, str(project.content))
                project_id = project.structured_content["data"]["id"]
                written = await client.call_tool("memweave_memory_write", {
                    "project_id": project_id, "title": "Rebuild boundary",
                    "body": "Markdown survives while the derived SQLite index can be rebuilt.",
                    "sources": ["host:test"],
                    "operation_id": "memory", "status": "active",
                })
                self.assertFalse(written.is_error, str(written.content))

            markdown_files = list((self.home / "knowledge").glob("*.md"))
            self.assertEqual(len(markdown_files), 1)
            original = markdown_files[0].read_bytes()
            shutil.rmtree(self.home / "index" / "basic-memory")

            async with self.client() as client:
                restored = await client.call_tool("memweave_memory_search", {
                    "project_id": project_id, "query": "derived SQLite index",
                })
                self.assertFalse(restored.is_error, str(restored.content))
                data = restored.structured_content["data"]
                self.assertEqual([item["title"] for item in data["items"]], ["Rebuild boundary"])
                self.assertEqual(data["coverage"]["strategy"], "basic_memory_sqlite_fts5")
            self.assertEqual(markdown_files[0].read_bytes(), original)

    async def test_graph_write_accepts_flat_source_and_relation_strings(self):
        root = Path(self.temporary.name) / "graph project"
        root.mkdir()
        (root / "main.py").write_text("print('graph')\n", encoding="utf-8")
        async with asyncio.timeout(30):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=str(root), name="graph flat",
                    operation_id="graph-project",
                )
                await call("project_scan", project_id=project["id"], operation_id="graph-scan")
                evidence = await call(
                    "evidence_read", project_id=project["id"], path="main.py", max_chars=20
                )
                written = await call(
                    "graph_write",
                    project_id=project["id"],
                    operation_id="graph-write",
                    title="Flat graph",
                    body="Architecture arguments stay flat for desktop clients.",
                    sources=[f"{evidence['path']}|{evidence['hash']}"],
                    relations=["entry|file:main.py|contains||confirmed"],
                )
                self.assertTrue(written["id"].startswith("architecture_"))
                graph = await call("graph_query", project_id=project["id"])
                node = next(
                    node for node in graph["nodes"] if node["id"] == written["id"]
                )
                self.assertEqual(node["title"], "Flat graph")
                self.assertEqual(node["revision"], 1)
                self.assertEqual(node["etag"], written["etag"])
                self.assertEqual(node["stale_reasons"], [])
                updated = await call(
                    "graph_update",
                    project_id=project["id"],
                    operation_id="graph-update",
                    architecture_id=written["id"],
                    expected_revision=1,
                    expected_etag=written["etag"],
                    title="Flat graph revised",
                    body="Updated architecture arguments remain flat.",
                    sources=[f"{evidence['path']}|{evidence['hash']}"],
                    relations=["entry|file:main.py|starts||confirmed"],
                )
                self.assertEqual(updated["revision"], 2)
                forgotten = await call(
                    "graph_forget",
                    project_id=project["id"],
                    operation_id="graph-forget",
                    architecture_id=written["id"],
                    expected_revision=2,
                    expected_etag=updated["etag"],
                )
                self.assertTrue(forgotten["deleted"])

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
