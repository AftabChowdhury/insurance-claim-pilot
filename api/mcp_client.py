"""Minimal MCP client: agents call tools on the ClaimPilot MCP servers over streamable HTTP."""
import json
import logging
from collections.abc import Callable
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.config import get_settings

log = logging.getLogger(__name__)


class MCPToolError(RuntimeError):
    pass


def _server_url(server: str) -> str:
    s = get_settings()
    return {"claims": s.claims_mcp_url, "policy": s.policy_mcp_url, "fraud": s.fraud_mcp_url}[server]


async def call_tool(server: str, tool: str, args: dict[str, Any]) -> Any:
    async with streamable_http_client(_server_url(server)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)

    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise MCPToolError(f"{server}.{tool}: {text}")
    sc = result.structured_content
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def call_tool_or_fallback(server: str, tool: str, args: dict, fallback: Callable[[], Any]) -> tuple[Any, bool]:
    """Call an MCP tool; if the server is not running, use the local fallback so the demo never breaks.
    Returns (result, used_mcp)."""
    try:
        return await call_tool(server, tool, args), True
    except Exception as e:  # noqa: BLE001 - connection errors come in many shapes
        log.warning("MCP %s.%s unavailable (%s); using local fallback", server, tool, e)
        return fallback(), False
