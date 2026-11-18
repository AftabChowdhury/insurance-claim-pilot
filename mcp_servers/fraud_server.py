"""fraud-mcp: fraud signals (claim history + a mock external watchlist), exposed as MCP tools.

Run: uv run python -m mcp_servers.fraud_server           (http://localhost:8003/mcp)
"""
import json
import os

from mcp.server.mcpserver import MCPServer

from core import db
from core.config import get_settings
from mcp_servers._run import run

mcp = MCPServer("claimpilot-fraud")
PORT = int(os.getenv("FRAUD_MCP_PORT", 8003))


def _watchlist() -> dict:
    path = get_settings().data_dir / "fraud_watchlist.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@mcp.tool()
def check_prior_claims(policy_number: str, exclude_claim_id: str | None = None) -> dict:
    """Prior claims on the same policy (excluding the current claim)."""
    claims = db.claim_history(policy_number, exclude_claim_id)
    return {"policy_number": policy_number, "count": len(claims), "claims": claims}


@mcp.tool()
def check_watchlist(policy_number: str) -> dict:
    """External fraud signals for a policy (mock data from data/fraud_watchlist.json)."""
    return {"policy_number": policy_number, "signals": _watchlist().get(policy_number, [])}


if __name__ == "__main__":
    db.init_db()
    run(mcp, PORT)
