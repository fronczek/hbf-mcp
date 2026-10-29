"""Actual stdio subprocess proves the explicit local identity transport path."""
import asyncio
import json
import os
from pathlib import Path
import pwd
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

def test_stdio_bound_unix_user_can_only_call_granted_tools():
    username = pwd.getpwuid(os.geteuid()).pw_name
    policy = {
        "version": 1,
        "roles": {"reader": {"tools": {"api_gitea_branches": ["hbf/allowed"]}}},
        "bindings": [{"subject": "local-reader", "client_id": "stdio-cli", "roles": ["reader"]}],
        "stdio_bindings": [{"unix_user": username, "subject": "local-reader", "client_id": "stdio-cli"}],
    }
    script = """
import json
import os
import pwd
from typing import Any
from auth.policy import Policy, current_principal
from auth.server import AuthorizedMCPServer
policy = Policy(json.loads(os.environ["HBF_TEST_POLICY"]))
username = pwd.getpwuid(os.geteuid()).pw_name
server = AuthorizedMCPServer("stdio-policy-test", policy=policy,
                             stdio_identity=policy.stdio_bindings[username])
@server.tool()
def api_gitea_branches(repo: str) -> dict[str, Any]:
    return {"repo": repo, "subject": current_principal.get().subject}
@server.tool()
def shell(command: str) -> dict[str, Any]:
    raise AssertionError("Administrative tool must never execute")
server.run(transport="stdio")
"""
    env = os.environ.copy()
    env.update(HBF_MCP_TRANSPORT="stdio", HBF_TEST_POLICY=json.dumps(policy),
               PYTHONPATH=str(ROOT))
    env.pop("HBF_MCP_AUDIT_FILE", None)
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-c", script], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                await client.initialize()
                assert [tool.name for tool in (await client.list_tools()).tools] == ["api_gitea_branches"]
                allowed = await client.call_tool("api_gitea_branches", {"repo": "hbf/allowed"})
                assert allowed.is_error is False
                assert allowed.structured_content["subject"] == "local-reader"
                denied = await client.call_tool("api_gitea_branches", {"repo": "hbf/other"})
                assert denied.is_error is True
                administrative = await client.call_tool("shell", {"command": "never executed"})
                assert administrative.is_error is True
    asyncio.run(run())
