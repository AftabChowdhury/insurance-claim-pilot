"""Shared launcher: streamable HTTP by default, or stdio with --stdio (for Claude Desktop / other MCP clients)."""
import os
import sys


def run(mcp, default_port: int) -> None:
    if "--stdio" in sys.argv:
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"), port=default_port)
