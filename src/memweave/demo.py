import asyncio
from pathlib import Path
from uuid import uuid4

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from memweave.diagnostics import DiagnosticError, result
from memweave.integrations import server_command
from memweave.services import Service


async def run_demo(home: Path, root: Path) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise DiagnosticError("RECORD_INVALID", "Demo root must be an existing directory.")
    Service(home).initialize()
    prefix = "demo-" + uuid4().hex

    def client():
        return Client(StdioServerParameters(**server_command(home)), read_timeout_seconds=15)

    async def call(connection, action, **parameters):
        response = await connection.call_tool("memweave_" + action, parameters)
        if response.is_error or not response.structured_content:
            raise DiagnosticError("DEMO_FAILED", f"Demo action failed: {action}.")
        return response.structured_content["data"]

    async with asyncio.timeout(60):
        async with client() as host_a:
            project = await call(host_a, "project_register", root=str(root.resolve()),
                                 name=root.name, operation_id=prefix + "-project")
            project_id = project["id"]
            await call(host_a, "workspace_prepare", root=str(root.resolve()), intent="inspect")
            task = await call(host_a, "task_open", project_id=project_id,
                              title="【演示】从工具 A 接续到工具 B", goal="记录检查点并在另一个服务进程中恢复",
                              acceptance=["恢复检查点", "检索并纠正知识"], operation_id=prefix + "-task")
            task_id = task["id"]
            work_units = [
                await call(
                    host_a, "work_unit_update", task_id=task_id, title=title,
                    owner=owner, status="running", progress="工具 A 已分派并行工作",
                    next_step="由工具 B 汇总结果", operation_id=prefix + "-" + suffix,
                )
                for title, owner, suffix in (
                    ("接口接续", "worker-backend", "unit-backend"),
                    ("页面接续", "worker-frontend", "unit-frontend"),
                )
            ]
            await call(host_a, "task_checkpoint", task_id=task_id, expected_revision=task["revision"],
                       expected_etag=task["etag"], findings="任务内容已写入本地 Markdown。",
                       progress="工具 A 已完成第一段工作。", next_step="由工具 B 恢复并验证知识检索。",
                       operation_id=prefix + "-checkpoint")
        async with client() as host_b:
            await call(host_b, "workspace_prepare", root=str(root.resolve()), intent="resume")
            restored = await call(host_b, "task_read", task_id=task_id)
            restart_readback = restored["next_step"] == "由工具 B 恢复并验证知识检索。"
            if {item["id"] for item in restored["work_units"]} != {item["id"] for item in work_units}:
                raise DiagnosticError("DEMO_FAILED", "WorkUnits did not survive the host switch.")
            for unit in restored["work_units"]:
                await call(
                    host_b, "work_unit_update", unit_id=unit["id"],
                    expected_revision=unit["revision"], expected_etag=unit["etag"],
                    status="completed", progress=f"{unit['owner']} 已完成",
                    verification="demo verification", operation_id=prefix + "-complete-" + unit["id"],
                )
            memory = await call(host_b, "memory_write", project_id=project_id,
                                title="【演示】任务接续方法", body="切换工具前保存发现、进展和明确的下一步。",
                                sources=[f"task:{task_id}"],
                                kind="lesson", status="active", operation_id=prefix + "-memory")
            await call(host_b, "memory_update", memory_id=memory["id"], expected_revision=memory["revision"],
                       expected_etag=memory["etag"], title="【演示】任务接续方法",
                       body="任务接续：切换工具前保存检查点；恢复时读取当前版本，并核对实际代码状态。",
                       operation_id=prefix + "-correction")
            hits = await call(host_b, "memory_search", project_id=project_id, query="接续")
            if not restart_readback or not any(item["id"] == memory["id"] for item in hits["items"]):
                raise DiagnosticError("DEMO_FAILED", "Restart or knowledge search verification failed.")
            disposable = await call(host_b, "memory_write", project_id=project_id,
                                     title="【演示】临时遗忘测试", body="这是一条用于验证遗忘的临时记录。",
                                     sources=["host:MCP demo"],
                                     status="active", operation_id=prefix + "-disposable")
            await call(host_b, "memory_forget", memory_id=disposable["id"],
                       expected_revision=disposable["revision"], expected_etag=disposable["etag"],
                       operation_id=prefix + "-forget")
            after = await call(host_b, "memory_search", project_id=project_id, query="临时遗忘")
            forgotten = not any(item["id"] == disposable["id"] for item in after["items"])
            scanned = await call(host_b, "project_scan", project_id=project_id, operation_id=prefix + "-scan")
            graph = await call(host_b, "graph_query", project_id=project_id, query="README.md", limit=100)
            source = next((node for node in graph["nodes"] if node["type"] == "file"), None)
            architecture_id = None
            if source:
                evidence = await call(host_b, "evidence_read", project_id=project_id, path=source["path"], max_chars=1000)
                architecture = await call(host_b, "graph_write", project_id=project_id,
                                          title="【演示】项目说明的来源", body="宿主记录项目说明文件的证据与结构关联；不代表自动语义理解。",
                                          sources=[f"{evidence['path']}|{evidence['hash']}"],
                                          relations=["self|file:" + evidence["path"] + "|documented_by"],
                                          operation_id=prefix + "-architecture")
                architecture_id = architecture["id"]
            phase = await call(
                host_b, "task_phase_close", task_id=task_id,
                expected_revision=restored["revision"], expected_etag=restored["etag"],
                title="实现与接续验证", summary="两个并行工作单元均完成，进入任务收口。",
                findings="WorkUnit 在独立 MCP 进程中可恢复。", verification="demo WorkUnit verification",
                next_step="结束任务并提炼", operation_id=prefix + "-phase",
            )
            after_phase = await call(host_b, "task_read", task_id=task_id)
            closed = await call(host_b, "task_close", task_id=task_id,
                       expected_revision=after_phase["revision"],
                       expected_etag=after_phase["etag"], outcome="completed",
                       summary="两个独立 stdio 服务进程完成了任务接续、知识检索与修订。",
                       acceptance_results=["第二个服务进程读回相同下一步。",
                                           "MCP 写入、修订后中文检索返回同一知识记录。"],
                       operation_id=prefix + "-close")
            await call(host_b, "memory_review_complete", task_id=task_id, expected_revision=closed["revision"],
                       expected_etag=closed["etag"], memory_ids=[memory["id"]],
                       summary="演示流程已保存并修订一条带任务来源的经验。", operation_id=prefix + "-review")
    return result({
        "verification_source": "sdk_stdio", "desktop_verification": "unverified",
        "project_id": project_id, "task_id": task_id, "memory_id": memory["id"],
        "work_unit_ids": [item["id"] for item in work_units], "phase_id": phase["phase_id"],
        "architecture_id": architecture_id, "restart_readback": restart_readback,
        "forgotten_verified": forgotten, "scanned_files": scanned["file_count"],
        "note": "Demo records remain in this home and are labelled 【演示】.",
    })
