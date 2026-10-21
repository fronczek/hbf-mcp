#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os

from mcp import Client


URL = os.getenv("HBF_MCP_TEST_URL", "http://127.0.0.1:8765/mcp")


async def main() -> None:
    async with Client(URL) as client:
        print(f"Connected to: {URL}")
        print(f"Protocol: {client.protocol_version}")
        print()

        result = await client.list_tools()
        print("Tools:")
        for tool in result.tools:
            annotations = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
            print(f"  - {tool.name}: {annotations}")

        print()
        info = await client.call_tool("info", {})
        if info.is_error:
            raise RuntimeError("MCP info failed")
        print("info:")
        print(info.structured_content)
        health = await client.call_tool("mcp_health", {})
        if health.is_error or not isinstance(health.structured_content, dict):
            raise RuntimeError("MCP health failed")
        state = health.structured_content
        if state.get("restart_required") is not False:
            raise RuntimeError("MCP runtime is stale or missing version diagnostics; check deployment")
        print("Runtime source matches disk:", state["disk_source_sha256"])


if __name__ == "__main__":
    asyncio.run(main())
