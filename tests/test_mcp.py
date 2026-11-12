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
                context_tool = next(tool for tool in tools if tool.name == "memweave_context_get")
                context_schema = context_tool.model_dump(by_alias=True)["inputSchema"]
                self.assertEqual(
                    context_schema["properties"]["include_knowledge_index"]["type"], "boolean"
                )
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
                registered = await client.call_tool("memweave_project_register", {
                    "root": self.temporary.name, "name": "errors", "operation_id": "error-project",
                })
                self.assertFalse(registered.is_error, str(registered.content))
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "inspect",
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
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
                names = {tool.name for tool in tools}
                self.assertEqual(len(tools), 22)
                self.assertNotIn("memweave_knowledge_index", names)
                # context_get is a diagnostic/CLI-only tool, not part of the 22-tool agent surface.
                self.assertNotIn("memweave_context_get", names)
                prepare = next(item for item in tools if item.name == "memweave_workspace_prepare")
                schema = prepare.model_dump(by_alias=True)["inputSchema"]
                flag = schema["properties"]["include_knowledge_index"]
                self.assertEqual(flag["type"], "boolean")
                self.assertNotIn("include_knowledge_index", schema.get("required", []))
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
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "inspect",
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
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

    async def test_workspace_prepare_injects_only_active_knowledge_index_when_requested(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="index flag",
                    operation_id="project",
                )
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
                task = await call(
                    "task_open", project_id=project["id"], title="index flag task",
                    goal="inject only the active navigation index", acceptance=["index visible"],
                    operation_id="task",
                )
                active = await call(
                    "memory_write", project_id=project["id"], title="ACTIVE_MCP_INDEX",
                    body="Active navigation entry from MCP.", sources=["host:test"],
                    operation_id="active",
                )
                await call(
                    "memory_activate", memory_id=active["id"],
                    expected_revision=active["revision"], expected_etag=active["etag"],
                    operation_id="activate",
                )
                candidate = await call(
                    "memory_write", project_id=project["id"], title="CANDIDATE_MCP_INDEX",
                    body="Candidate entry must stay out.", sources=["host:test"],
                    operation_id="candidate",
                )
                default = await call(
                    "workspace_prepare", root=self.temporary.name, intent="resume",
                )
                enabled = await call(
                    "workspace_prepare", root=self.temporary.name, intent="resume",
                    include_knowledge_index=True,
                )
                self.assertEqual(default["task_id"], task["id"])
                self.assertNotIn("knowledge_index", default["context"])
                metadata = enabled["context"]["knowledge_index"]
                self.assertTrue(metadata["injected"])
                text = enabled["context"]["text"]
                self.assertIn("knowledge index", text.lower())
                self.assertIn("- " + active["id"] + " | ", text)
                self.assertNotIn(candidate["id"], text)

    async def test_task_and_memory_survive_host_process_switch(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]
                project = await call("project_register", root=self.temporary.name, name="切换演示", operation_id="p")
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
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
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "resume", "query": "交接", "budget": 2000,
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
                self.assertEqual(prepared.structured_content["data"]["task_id"], task["id"])
                self.assertIn("运行验证", prepared.structured_content["data"]["context"]["text"])
                task_response = await client.call_tool("memweave_task_read", {"task_id": task["id"]})
                self.assertEqual(task_response.structured_content["data"]["next_step"], "运行验证")
                self.assertEqual(
                    task_response.structured_content["data"]["work_units"][0]["id"], unit["id"]
                )
                latest_task = task_response.structured_content["data"]
                progress_only = await client.call_tool("memweave_task_checkpoint", {
                    "task_id": task["id"], "expected_revision": latest_task["revision"],
                    "expected_etag": latest_task["etag"], "progress": "仅更新进展",
                    "next_step": "运行验证", "operation_id": "stdio-progress-only",
                })
                self.assertFalse(progress_only.is_error, str(progress_only.content))
                preserved = await client.call_tool("memweave_task_read", {"task_id": task["id"]})
                self.assertEqual(preserved.structured_content["data"]["findings"], "已定位")
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
                corrected = await client.call_tool("memweave_memory_update", {
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
                retired = await client.call_tool("memweave_memory_update", {
                    "memory_id": candidate["id"],
                    "expected_revision": corrected.structured_content["data"]["revision"],
                    "expected_etag": corrected.structured_content["data"]["etag"],
                    "title": "交接规则已取消", "body": "用户取消了这条交接规则。",
                    "status": "retired", "operation_id": "retire",
                })
                self.assertFalse(retired.is_error, str(retired.content))
                default_search = await client.call_tool("memweave_memory_search", {
                    "project_id": project["id"], "query": "交接",
                })
                self.assertEqual(default_search.structured_content["data"]["matched"], 0)
                history = await client.call_tool("memweave_memory_search", {
                    "project_id": project["id"], "query": "交接", "status": "retired",
                })
                self.assertEqual(history.structured_content["data"]["items"][0]["id"], candidate["id"])

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
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
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
                await call(first_client, "workspace_prepare", root=self.temporary.name, intent="inspect")
                await call(second_client, "workspace_prepare", root=self.temporary.name, intent="inspect")
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

    async def test_two_stdio_processes_cannot_both_drive_the_main_task(self):
        async with asyncio.timeout(60):
            async with self.client() as first_client, self.client() as second_client:
                async def call(client, action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    first_client, "project_register", root=self.temporary.name,
                    name="execution ownership", operation_id="ownership-project",
                )
                await call(first_client, "workspace_prepare", root=self.temporary.name, intent="inspect")
                self.assertFalse((self.home / "runtime" / "execution").exists())
                task = await call(
                    first_client, "task_open", project_id=project["id"],
                    title="唯一驱动者", goal="阻止第二个服务实例写主任务",
                    acceptance=["第二个实例只能创建侧任务"],
                    operation_id="ownership-task",
                )
                claimed = await call(
                    first_client, "workspace_prepare", root=self.temporary.name, intent="resume",
                )
                self.assertEqual(claimed["execution"]["state"], "acquired")
                self.assertTrue(claimed["execution"]["claim_id"])

                blocked = await call(
                    second_client, "workspace_prepare", root=self.temporary.name, intent="resume",
                )
                self.assertEqual(blocked["state"], "task_claimed")
                self.assertIsNone(blocked["task_id"])
                self.assertIsNone(blocked["context"])
                self.assertEqual(blocked["tasks"], [])
                self.assertEqual(blocked["background_task"]["id"], task["id"])
                blocked_write = await second_client.call_tool("memweave_task_checkpoint", {
                    "task_id": task["id"], "expected_revision": task["revision"],
                    "expected_etag": task["etag"], "progress": "must not write",
                    "next_step": "must not continue", "operation_id": "blocked-main-write",
                })
                self.assertTrue(blocked_write.is_error)
                self.assertIn("EXECUTION_CLAIM_REQUIRED", blocked_write.content[0].text)

            async with self.client() as replacement_client:
                resumed = await replacement_client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "resume",
                })
                self.assertFalse(resumed.is_error, str(resumed.content))
                resumed_data = resumed.structured_content["data"]
                self.assertEqual(resumed_data["state"], "ready")
                self.assertEqual(resumed_data["execution"]["state"], "acquired")
                self.assertNotEqual(resumed_data["execution"]["claim_id"], claimed["execution"]["claim_id"])

    async def test_one_process_rebuilds_search_from_markdown_after_restart(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                project = await client.call_tool("memweave_project_register", {
                    "root": self.temporary.name, "name": "rebuild", "operation_id": "project",
                })
                self.assertFalse(project.is_error, str(project.content))
                project_id = project.structured_content["data"]["id"]
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "inspect",
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
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
                prepared = await client.call_tool("memweave_workspace_prepare", {
                    "root": self.temporary.name, "intent": "inspect",
                })
                self.assertFalse(prepared.is_error, str(prepared.content))
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
                await call("workspace_prepare", root=str(root), intent="inspect")
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

    async def test_flat_evidence_strings_round_trip_over_stdio(self):
        expected_checkpoint_evidence = [
            {
                "kind": "command",
                "reference": "python -m unittest tests.test_mcp",
                "hash": "abc123",
                "observed_at": "2026-11-08T10:00:00+08:00",
                "summary": "focused stdio suite passed",
            },
            {"kind": "file", "reference": "src/memweave/server.py"},
            {"kind": "user", "reference": "用户要求扁平字符串保持兼容"},
        ]
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="evidence flat",
                    operation_id="evidence-project",
                )
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
                task = await call(
                    "task_open", project_id=project["id"], title="Flat evidence",
                    goal="Keep desktop-compatible arguments", acceptance=["round trip"],
                    operation_id="evidence-task",
                )
                await call(
                    "task_checkpoint", task_id=task["id"],
                    expected_revision=task["revision"], expected_etag=task["etag"],
                    findings="evidence collected", progress="implemented", next_step="verify",
                    evidence=[
                        "command|python -m unittest tests.test_mcp|abc123|"
                        "2026-11-08T10:00:00+08:00|focused stdio suite passed",
                        "file|src/memweave/server.py",
                        "user|用户要求扁平字符串保持兼容|",
                    ],
                    operation_id="evidence-checkpoint",
                )
                detail = await call("task_read", task_id=task["id"])
                self.assertEqual(detail["evidence"], expected_checkpoint_evidence)
                self.assertEqual(detail["checkpoints"][0]["evidence"], expected_checkpoint_evidence)

                memory = await call(
                    "memory_write", project_id=project["id"], title="Evidence rule",
                    body="Structured evidence is flattened only at the MCP boundary.",
                    sources=["host:stdio"], status="active",
                    evidence=[
                        "test|tests.test_mcp",
                        "url|https://example.test/doc|hash1|2026-11-08",
                    ],
                    operation_id="evidence-memory",
                )
                hits = await call(
                    "memory_search", project_id=project["id"], query="Evidence rule"
                )
                self.assertEqual(hits["items"][0]["evidence"], [
                    {"kind": "test", "reference": "tests.test_mcp"},
                    {"kind": "url", "reference": "https://example.test/doc", "hash": "hash1",
                     "observed_at": "2026-11-08"},
                ])
                corrected = await call(
                    "memory_update", memory_id=memory["id"], expected_revision=memory["revision"],
                    expected_etag=memory["etag"], title="Evidence rule",
                    body="Updated wording.", evidence=["file|src/memweave/services.py"],
                    operation_id="evidence-update",
                )
                corrected_hits = await call(
                    "memory_search", project_id=project["id"], query="Evidence rule"
                )
                self.assertEqual(
                    corrected_hits["items"][0]["evidence"],
                    [{"kind": "file", "reference": "src/memweave/services.py"}],
                )
                # Old clients that omit evidence stay compatible and read back an empty list.
                legacy = await call(
                    "memory_write", project_id=project["id"], title="Legacy call",
                    body="No evidence argument is sent here.", sources=["host:legacy"],
                    status="active", operation_id="legacy-memory",
                )
                legacy_hits = await call(
                    "memory_search", project_id=project["id"], query="Legacy call"
                )
                self.assertEqual(
                    [item["id"] for item in legacy_hits["items"]], [legacy["id"]]
                )
                self.assertEqual(legacy_hits["items"][0]["evidence"], [])
                # A malformed flat item is a tool error and leaves the task revision unchanged.
                before = await call("task_read", task_id=task["id"])
                invalid = await client.call_tool("memweave_task_checkpoint", {
                    "task_id": task["id"], "expected_revision": before["revision"],
                    "expected_etag": before["etag"], "progress": "should not write",
                    "next_step": "should not write", "evidence": ["missing-kind"],
                    "operation_id": "invalid-evidence",
                })
                self.assertTrue(invalid.is_error)
                self.assertIn("evidence", invalid.content[0].text)
                unchanged = await call("task_read", task_id=task["id"])
                self.assertEqual(unchanged["revision"], before["revision"])
                self.assertEqual(unchanged["checkpoints"][0]["evidence"], expected_checkpoint_evidence)

    async def test_legacy_workspace_and_checkpoint_aliases_are_accepted(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="aliases",
                    operation_id="alias-project",
                )
                inspected = await call(
                    "workspace_prepare", cwd=self.temporary.name, mode="inspect",
                )
                self.assertEqual(inspected["state"], "ready")
                task = await call(
                    "task_open", project_id=project["id"], title="alias task",
                    goal="accept legacy field names", acceptance=["checkpoint works"],
                    operation_id="alias-task",
                )
                resumed = await call(
                    "workspace_prepare", execution_root=self.temporary.name,
                    task_id=task["id"], budget=400,
                )
                self.assertEqual(resumed["task_id"], task["id"])
                checkpoint = await call(
                    "task_checkpoint", task_id=task["id"], revision=task["revision"],
                    etag=task["etag"], progress="aliases normalized",
                    next_step="continue", operation_id="alias-checkpoint",
                )
                self.assertGreater(checkpoint["revision"], task["revision"])

    async def test_task_close_accepts_outcome_alias_and_text_results(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="close aliases",
                    operation_id="close-project",
                )
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
                task = await call(
                    "task_open", project_id=project["id"], title="text close",
                    goal="close with compact arguments",
                    acceptance=["first criterion", "second criterion"],
                    operation_id="close-task",
                )
                closed = await call(
                    "task_close", task_id=task["id"], expected_revision=task["revision"],
                    expected_etag=task["etag"], outcome="success", summary="closed",
                    acceptance_results_text="first passed\n---\nsecond passed",
                    operation_id="close-now",
                )
                self.assertEqual(closed["status"], "completed")
                detail = await call("task_read", task_id=task["id"])
                self.assertEqual(detail["status"], "completed")
                self.assertEqual(
                    detail["acceptance_results"],
                    {"first criterion": "first passed", "second criterion": "second passed"},
                )
                next_task = await call(
                    "task_open", project_id=project["id"], title="string close",
                    goal="close with one string",
                    acceptance=["alpha criterion", "beta criterion"],
                    operation_id="string-close-task",
                )
                string_closed = await call(
                    "task_close", task_id=next_task["id"],
                    expected_revision=next_task["revision"], expected_etag=next_task["etag"],
                    outcome="done", summary="closed from a string",
                    acceptance_results="alpha passed\n---\nbeta passed",
                    operation_id="string-close-now",
                )
                self.assertEqual(string_closed["status"], "completed")

    async def test_task_close_empty_arguments_return_recovery_error(self):
        async with asyncio.timeout(20):
            async with self.client() as client:
                empty = await client.call_tool("memweave_task_close", {})
                self.assertTrue(empty.is_error)
                message = empty.content[0].text
                self.assertIn("No task was closed", message)
                self.assertIn("task_read", message)
                self.assertIn("reload the window", message)

                partial = await client.call_tool("memweave_task_close", {
                    "task_id": "task_missing",
                    "summary": "should not close",
                    "operation_id": "partial-close",
                })
                self.assertTrue(partial.is_error)
                partial_message = partial.content[0].text
                self.assertIn("No task was closed", partial_message)
                self.assertIn("expected_revision", partial_message)

    async def test_plain_sources_and_unchanged_work_unit_metadata_are_accepted(self):
        async with asyncio.timeout(40):
            async with self.client() as client:
                async def call(action, **parameters):
                    response = await client.call_tool("memweave_" + action, parameters)
                    self.assertFalse(response.is_error, str(response.content))
                    return response.structured_content["data"]

                project = await call(
                    "project_register", root=self.temporary.name, name="source aliases",
                    operation_id="source-project",
                )
                await call("workspace_prepare", root=self.temporary.name, intent="inspect")
                task = await call(
                    "task_open", project_id=project["id"], title="source task",
                    goal="accept plain references", acceptance=["memory written"],
                    operation_id="source-task",
                )
                memory = await call(
                    "memory_write", project_id=project["id"], title="plain references",
                    body="Plain paths and references are normalized at the MCP boundary.",
                    sources=[
                        "src/memweave/server.py", task["id"],
                        "checkpoint_abcdef123456", "https://example.test/doc", "deadbee",
                    ],
                    operation_id="plain-sources",
                )
                self.assertEqual(memory["status"], "candidate")
                hits = await call(
                    "memory_search", project_id=project["id"], query="plain references",
                    status="candidate",
                )
                self.assertEqual(hits["items"][0]["sources"], [
                    {"type": "file", "reference": "src/memweave/server.py"},
                    {"type": "task", "reference": task["id"]},
                    {"type": "task", "reference": "checkpoint_abcdef123456"},
                    {"type": "host", "reference": "https://example.test/doc"},
                    {"type": "host", "reference": "commit:deadbee"},
                ])
                unit = await call(
                    "work_unit_update", task_id=task["id"], title="unchanged unit",
                    owner="worker", status="running", progress="started",
                    next_step="continue", operation_id="unit-create",
                )
                updated = await call(
                    "work_unit_update", unit_id=unit["id"], title="unchanged unit",
                    owner="worker", status="running",
                    expected_revision=unit["revision"], expected_etag=unit["etag"],
                    progress="still running", next_step="continue",
                    operation_id="unit-update",
                )
                self.assertGreater(updated["revision"], unit["revision"])
                changed = await client.call_tool("memweave_work_unit_update", {
                    "unit_id": unit["id"], "title": "changed title", "owner": "worker",
                    "status": "running", "expected_revision": updated["revision"],
                    "expected_etag": updated["etag"], "progress": "must not write",
                    "next_step": "must not write", "operation_id": "unit-change-title",
                })
                self.assertTrue(changed.is_error)
                self.assertIn("immutable", changed.content[0].text)

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
