import asyncio
from pathlib import Path
from uuid import uuid4

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from memweave.diagnostics import Diagnostics, DiagnosticError, result
from memweave.integrations import server_command


def probe_data(response) -> dict:
    if response.is_error or response.structured_content is None:
        raise DiagnosticError("PROBE_FAILED", "MCP tool failed or returned no structured content.")
    return response.structured_content["data"]


async def run_probe(home: Path) -> dict:
    if not Diagnostics(home).status()["data"]["initialized"]:
        raise DiagnosticError("NOT_INITIALIZED", "Run memweave init before the probe.")
    parameters = StdioServerParameters(**server_command(home, toolset="full"))
    operation_id = "stdio-" + uuid4().hex
    marker = "MemWeave · 织忆 · restart readback"
    try:
        async with asyncio.timeout(30):
            async with Client(parameters, read_timeout_seconds=10) as client:
                tools = await client.list_tools()
                names = sorted(tool.name for tool in tools.tools)
                expected = {"memweave_system_status", "memweave_probe_write", "memweave_probe_read"}
                if not expected.issubset(names):
                    raise DiagnosticError("PROBE_FAILED", "Server is missing required probe tools.")
                protocol_version = client.protocol_version
                probe_data(await client.call_tool("memweave_system_status"))
                written = probe_data(await client.call_tool(
                    "memweave_probe_write", {"operation_id": operation_id, "marker": marker}
                ))
                if written["marker"] != marker:
                    raise DiagnosticError("PROBE_FAILED", "Written marker did not round-trip.")
            async with Client(parameters, read_timeout_seconds=10) as client:
                restored = probe_data(await client.call_tool(
                    "memweave_probe_read", {"operation_id": operation_id}
                ))
                if restored["marker"] != marker:
                    raise DiagnosticError("PROBE_FAILED", "Restart readback did not match.")
    except DiagnosticError:
        raise
    except TimeoutError as error:
        raise DiagnosticError("PROBE_TIMEOUT", "Stdio probe exceeded 30 seconds.") from error
    except Exception as error:
        raise DiagnosticError(
            "PROBE_FAILED", "Stdio negotiation or tool call failed; inspect server stderr."
        ) from error
    return result({
        "verification_source": "sdk_stdio",
        "protocol_version": protocol_version,
        "tools": names,
        "operation_id": operation_id,
        "restart_readback": True,
        "desktop_verification": "unverified",
    })
