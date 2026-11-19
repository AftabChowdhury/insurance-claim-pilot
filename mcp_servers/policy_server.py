"""policy-mcp: hybrid + semantic search over insurance policies (Azure AI Search), exposed as MCP tools.

The LangGraph pipeline calls core.search directly for speed and per-step tracing; this server exposes the
same retrieval to any MCP client (Claude Desktop, other agents). It only ever serves public clauses: chunks of
documents marked confidential in the Admin panel are filtered out (core.search access="public").

Run: uv run python -m mcp_servers.policy_server          (http://localhost:8002/mcp)
"""
import os

from mcp.server.mcpserver import MCPServer

from core import search
from mcp_servers._run import run

mcp = MCPServer("claimpilot-policy")
PORT = int(os.getenv("POLICY_MCP_PORT", 8002))


@mcp.tool()
def search_policy(query: str, policy_type: str | None = None, top: int = 5) -> dict:
    """Search insurance policy clauses with hybrid search (BM25 + vector) and the semantic ranker.
    policy_type: 'home' or 'auto' (optional)."""
    hits = search.search_policy(query, policy_type, "hybrid_semantic", top=50, access="public")
    hits = sorted(hits, key=lambda h: h.get("reranker_score") or 0, reverse=True)[:top]
    return {"query": query, "results": [
        {"clause_id": h["clause_id"], "title": h["title"], "policy_type": h["policy_type"],
         "reranker_score": h["reranker_score"], "content": h["content"]} for h in hits]}


@mcp.tool()
def get_clause(clause_id: str, policy_type: str | None = None) -> dict:
    """Get the full text of one clause by ID, e.g. '7.3' or 'E-2'."""
    c = search.get_clause(clause_id, policy_type, access="public")
    return c or {"error": f"Clause {clause_id} not found"}


if __name__ == "__main__":
    run(mcp, PORT)
