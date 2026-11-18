"""Advanced RAG nodes: query rewrite -> hybrid search -> semantic ranking -> context building."""
import asyncio
import json

from core.config import get_settings
from core.llm import structured
from core.search import search_policy

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
    Results from all queries are merged by chunk id, keeping the best scores."""
    policy_type = state.get("policy_type")
    results = await asyncio.gather(*[
        asyncio.to_thread(search_policy, q, policy_type, "hybrid_semantic") for q in state["queries"]
    ])
    merged: dict[str, dict] = {}
    for q, hits in zip(state["queries"], results):
        for h in hits:
            cur = merged.get(h["id"])
            if cur is None:
                merged[h["id"]] = {**h, "matched_queries": [q]}
            else:
                cur["matched_queries"].append(q)
                cur["score"] = max(cur["score"] or 0, h["score"] or 0)
                cur["reranker_score"] = max(cur["reranker_score"] or 0, h["reranker_score"] or 0)
    candidates = list(merged.values())
    total = sum(len(r) for r in results)
    return {"candidates": candidates,
            **event("hybrid_search", f"{len(state['queries'])} queries → {total} hits → {len(candidates)} unique chunks",
                    policy_type=policy_type, hits=total, unique=len(candidates))}


async def semantic_rank(state: ClaimState) -> dict:
    s = get_settings()
    top_k = state.get("top_k") or s.retrieval_top_k
    min_score = state.get("min_reranker_score", s.min_reranker_score)
    ordered = sorted(state.get("candidates", []), key=lambda c: c.get("reranker_score") or 0, reverse=True)
    ranked = [c for c in ordered if (c.get("reranker_score") or 0) >= min_score][:top_k]
    if not ranked:
        ranked = ordered[:3]  # keep something so the agents can say "unclear"
    top = [{"clause": c["clause_id"], "title": c["title"], "reranker_score": round(c.get("reranker_score") or 0, 2)}
           for c in ranked]
    msg = "top: " + ", ".join(f"{t['clause']} ({t['reranker_score']})" for t in top[:3]) if top else "no relevant chunks"
    return {"ranked": ranked, **event("semantic_rank", msg, top=top, top_k=top_k, min_score=min_score)}


async def build_context(state: ClaimState) -> dict:
    """Expand to parent sections, de-duplicate, keep within budget, tag every block with its clause ID."""
    budget = get_settings().context_char_budget
    blocks, seen_sections, clauses, used = [], set(), [], 0
    for c in state.get("ranked", []):
        block = f"[Clause {c['clause_id']}] {c['title']}\n{c['content']}"
        parent = c.get("parent_content")
        section_key = (c.get("source"), c.get("section"))
        if parent and section_key not in seen_sections and used + len(parent) < budget * 0.6:
            block += f"\n(Section context – {c.get('section')}):\n{parent}"
            seen_sections.add(section_key)
        if used + len(block) > budget:
            break
        blocks.append(block)
        clauses.append(c["clause_id"])
        used += len(block)
    context = "\n\n---\n\n".join(blocks) if blocks else "(no relevant policy text found)"
    return {"context": context, "context_clauses": clauses,
            **event("build_context", f"{len(blocks)} blocks · ~{used // 4} tokens · clauses {', '.join(clauses) or '—'}",
                    clauses=clauses, chars=used)}
