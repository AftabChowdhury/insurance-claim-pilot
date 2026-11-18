"""claims-mcp: the claims system of record, exposed as MCP tools.

Run: uv run python -m mcp_servers.claims_server          (http://localhost:8001/mcp)
     uv run python -m mcp_servers.claims_server --stdio  (for Claude Desktop)
"""
import os

from mcp.server.mcpserver import MCPServer

from core import db
from mcp_servers._run import run

mcp = MCPServer("claimpilot-claims")
PORT = int(os.getenv("CLAIMS_MCP_PORT", 8001))


@mcp.tool()
def get_claim(claim_id: str) -> dict:
    """Get a claim by ID (status, amount, payout, documents, decision letter)."""
    c = db.get_claim(claim_id)
    return c or {"error": f"Claim {claim_id} not found"}


@mcp.tool()
def list_claims(status: str | None = None, limit: int = 20) -> dict:
    """List recent claims, optionally filtered by status (RECEIVED, APPROVED, DENIED, PENDING_INFO, ESCALATED, FLAGGED_SECURITY)."""
    return {"claims": [{k: c[k] for k in ("id", "policy_number", "loss_type", "amount", "status", "payout", "created_at")}
                       for c in db.list_claims(status, limit)]}


@mcp.tool()
def update_claim_status(claim_id: str, status: str, payout: float | None = None,
                        decided_by: str = "claimpilot", note: str = "") -> dict:
    """Set the final status and payout of a claim. Writes an audit entry."""
    if not db.get_claim(claim_id):
        return {"error": f"Claim {claim_id} not found"}
    c = db.update_status(claim_id, status, payout, decided_by, note)
    return {"claim_id": claim_id, "status": c["status"], "payout": c["payout"]}


@mcp.tool()
def save_decision_letter(claim_id: str, letter: str) -> dict:
    """Store the customer decision letter for a claim."""
    if not db.get_claim(claim_id):
        return {"error": f"Claim {claim_id} not found"}
    db.save_letter(claim_id, letter)
    return {"claim_id": claim_id, "saved": True, "chars": len(letter)}


@mcp.tool()
def get_audit_trail(claim_id: str) -> dict:
    """Full audit trail for a claim: who did what and when (including human reviews)."""
    return {"claim_id": claim_id, "entries": db.get_audit(claim_id)}


if __name__ == "__main__":
    db.init_db()
    run(mcp, PORT)
