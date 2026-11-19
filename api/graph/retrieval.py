"""Advanced RAG nodes: query rewrite -> hybrid search -> semantic ranking -> context building."""
import json

from core import rag
from core.config import get_settings
from core.llm import structured

from . import prompts
from .schemas import SearchQueries
from .state import ClaimState, event


async def rewrite_query(state: ClaimState) -> dict:
    retry = state.get("retries", 0)
    retry_block = ""
    if retry:
        retry_block = prompts.RETRY_BLOCK.format(
            retry=retry, previous=json.dumps(state.get("queries", [])), feedback=state.get("feedback", ""))
    out: SearchQueries = await structured(SearchQueries, mini=True).ainvoke(
        prompts.REWRITE.format(claim=json.dumps(state.get("claim", {}), indent=2), retry_block=retry_block))
    queries = out.queries[:5]
    return {"queries": queries, **event("rewrite_query", f"{len(queries)} sub-queries", queries=queries, attempt=retry + 1)}


async def hybrid_search(state: ClaimState) -> dict:
    """Each query runs BM25 + vector (fused by RRF) with the semantic ranker in one Azure AI Search call.
    Results from all queries are merged by chunk id, keeping the best scores.

    The claims workflow is internal (adjuster-reviewed), so it also searches confidential documents
    (access="all"); their clauses are labelled in the context and kept out of the customer letter."""
    policy_type = state.get("policy_type")
    results = await rag.multi_search(state["queries"], policy_type, access="all")
    candidates = rag.merge_hits(state["queries"], results)
    total = sum(len(r) for r in results)
    return {"candidates": candidates,
            **event("hybrid_search", f"{len(state['queries'])} queries → {total} hits → {len(candidates)} unique chunks",
                    policy_type=policy_type, hits=total, unique=len(candidates))}


async def semantic_rank(state: ClaimState) -> dict:
    s = get_settings()
    top_k = state.get("top_k") or s.retrieval_top_k
    min_score = state.get("min_reranker_score", s.min_reranker_score)
    ranked = rag.rank(state.get("candidates", []), top_k, min_score, keep_if_empty=3)  # agents can say "unclear"
    top = [{"clause": c["clause_id"], "title": c["title"], "reranker_score": round(c.get("reranker_score") or 0, 2)}
           for c in ranked]
    msg = "top: " + ", ".join(f"{t['clause']} ({t['reranker_score']})" for t in top[:3]) if top else "no relevant chunks"
    return {"ranked": ranked, **event("semantic_rank", msg, top=top, top_k=top_k, min_score=min_score)}


async def build_context(state: ClaimState) -> dict:
    """Expand to parent sections, de-duplicate, keep within budget, tag every block with its clause ID.
    public_context (without confidential clauses) is what the letter writer sees."""
    ctx = rag.build_context(state.get("ranked", []), get_settings().context_char_budget)
    msg = f"{ctx.blocks} blocks · ~{ctx.chars // 4} tokens · clauses {', '.join(ctx.clauses) or '—'}"
    if ctx.confidential_clauses:
        msg += f" · {len(ctx.confidential_clauses)} confidential (internal only)"
    return {"context": ctx.text, "context_clauses": ctx.clauses, "public_context": ctx.public_text,
            "confidential_clauses": ctx.confidential_clauses,
            **event("build_context", msg, clauses=ctx.clauses, chars=ctx.chars,
                    confidential_clauses=ctx.confidential_clauses)}
