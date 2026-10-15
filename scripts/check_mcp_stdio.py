"""Check MCP initialization and schemas without calling any CAE bridge."""

import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "mcp_server.py")],
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            tools = (await session.list_tools()).tools
            resources = (await session.list_resources()).resources
            assert initialized.serverInfo.name == "wmw-abaqus-mcp"
            expected = {
                "discover_sessions", "list_sessions", "set_active_session", "ping",
                "check_abaqus_connection", "run_python", "execute_script", "set_workdir",
                "get_model_info", "list_jobs", "submit_job", "monitor_job_status",
                "inspect_odb", "get_odb_info", "capture_viewport", "get_viewport_image",
            }
            assert {tool.name for tool in tools} == expected
            session_tools = {"discover_sessions", "list_sessions", "set_active_session"}
            for tool in tools:
                if tool.name not in session_tools:
                    assert "session_id" in tool.inputSchema["properties"], tool.name
            assert {str(resource.uri) for resource in resources} == {
                "abaqus://sessions", "abaqus://status", "abaqus://session-telemetry",
                "abaqus://agent-instructions",
            }
            print(json.dumps({
                "server": initialized.serverInfo.name,
                "tools": len(tools), "resources": len(resources),
                "cae_tools_accept_session_id": True,
                "bridge_calls": 0,
            }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
