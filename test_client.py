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
            print(f"  - {tool.name}")

        print()
        info = await client.call_tool("info", {})
        print("info:")
        print(info.structured_content)


if __name__ == "__main__":
    asyncio.run(main())
