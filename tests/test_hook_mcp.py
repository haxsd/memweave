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

from memweave.integrations import server_command
from memweave.services import Service


class HookMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_results_drive_checkpoint_consolidation_and_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "项目"
            root.mkdir()
            home = Path(directory) / "memory home"
            Service(home).initialize()

            async def hook(event, **parameters):
                payload = {"conversation_id": "sdk-session", "generation_id": "turn-1",
                           "workspace_roots": [str(root)], **parameters}
                execution = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "memweave", "hook", "--client", "cursor",
                     "--event", event, "--home", str(home)],
                    input=json.dumps(payload), encoding="utf-8", capture_output=True,
                    env={**os.environ, "PYTHONUTF8": "1"}, cwd=directory, timeout=15,
                )
                self.assertEqual(execution.returncode, 0, execution.stderr)
                self.assertEqual(execution.stderr, "")
                return json.loads(execution.stdout)

            async with asyncio.timeout(50):
                async with Client(StdioServerParameters(**server_command(home), cwd=directory)) as client:
                    async def call(action, **parameters):
                        response = await client.call_tool("memweave_" + action, parameters)
                        self.assertFalse(response.is_error, str(response.content))
                        await hook("afterMCPExecution", tool_name="memweave_" + action,
                                   mcp_server_name="memweave", tool_input=json.dumps(parameters),
                                   result_json=response.model_dump_json(by_alias=True))
                        return response.structured_content["data"]

                    project = await call("project_register", root=str(root), name="SDK Hook", operation_id="p")
                    await hook("sessionStart")
                    task = await call("task_open", project_id=project["id"], title="修复",
                                      goal="验证自动记忆维护", acceptance=["回归"], operation_id="t")
                    reminder = await hook("stop", status="completed", loop_count=0)
                    self.assertIn("memweave_task_checkpoint", reminder.get("followup_message", ""))
                    saved = await call("task_checkpoint", task_id=task["id"], expected_revision=1,
                                       expected_etag=task["etag"], findings="SDK 真实响应可关联",
                                       progress="测试环境已执行验证", next_step="关闭并提炼", operation_id="cp")
                    self.assertEqual(await hook("stop", status="completed", loop_count=1), {})
                    closed = await call("task_close", task_id=task["id"], expected_revision=saved["revision"],
                                        expected_etag=saved["etag"], outcome="completed", summary="已验证",
                                        acceptance_results={"回归": "测试环境通过"}, operation_id="close")
                    review = await hook("stop", status="completed", loop_count=0, generation_id="turn-2")
                    self.assertIn("memweave_memory_review", review.get("followup_message", ""))
                    await call("memory_review", task_id=task["id"])
                    memory = await call("memory_write", project_id=project["id"], title="已确认的项目经验",
                                        body="NEXT_SESSION_REUSES_KNOWLEDGE", operation_id="knowledge",
                                        sources=[{"type": "task", "reference": task["id"]}])
                    await call("memory_review_complete", task_id=task["id"], expected_revision=closed["revision"],
                               expected_etag=closed["etag"], memory_ids=[memory["id"]],
                               summary="已核对并保存", operation_id="review")
                    self.assertEqual(await hook("stop", status="completed", loop_count=1), {})

                await hook("sessionEnd")
                restored = await hook("sessionStart", conversation_id="next-session")
                self.assertIn("NEXT_SESSION_REUSES_KNOWLEDGE", restored["additional_context"])
                self.assertNotIn("关闭并提炼", restored["additional_context"])


if __name__ == "__main__":
    unittest.main()
