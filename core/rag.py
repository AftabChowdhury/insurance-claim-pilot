"""Shared advanced-RAG helpers for the claims workflow and the policy chat.

    hits = await multi_search(queries, policy_type, access)   # hybrid (BM25 + vector, RRF) + semantic ranker
    candidates = merge_hits(queries, hits)                    # one entry per chunk, best scores kept
    ranked = rank(candidates, top_k, min_score)               # sort by reranker score, threshold, top k
    ctx = build_context(ranked, budget)                       # [Clause N.M] blocks + parent section context
"""
import asyncio
from dataclasses import dataclass, field

from core.search import Access, search_policy


async def multi_search(queries: list[str], policy_type: str | list[str] | None, access: Access, top: int | None = None,
                       fields: list[str] | None = None) -> list[list[dict]]:
    """Each query is one Azure AI Search call: BM25 + vector fused by RRF, re-scored by the semantic ranker."""
    return await asyncio.gather(*[
        asyncio.to_thread(search_policy, q, policy_type, "hybrid_semantic", top, access, fields) for q in queries
    ])


def merge_hits(queries: list[str], results: list[list[dict]]) -> list[dict]:
    """Merge the results of several queries by chunk id, keeping the best scores and the matching queries."""
    merged: dict[str, dict] = {}
    for q, hits in zip(queries, results):
        for h in hits:
            cur = merged.get(h["id"])
            if cur is None:
                merged[h["id"]] = {**h, "matched_queries": [q]}
            else:
                cur["matched_queries"].append(q)
                cur["score"] = max(cur["score"] or 0, h["score"] or 0)
                cur["reranker_score"] = max(cur["reranker_score"] or 0, h["reranker_score"] or 0)
    return list(merged.values())


def best_score(hits: list[dict]) -> float:
    return max((h.get("reranker_score") or 0 for h in hits), default=0.0)


def rank(candidates: list[dict], top_k: int, min_score: float, keep_if_empty: int = 0) -> list[dict]:
    """Semantic ranking: sort by reranker score, keep chunks >= min_score, at most top_k.
    keep_if_empty > 0 keeps that many best chunks when none pass (so an agent can still say "unclear")."""
    ordered = sorted(candidates, key=lambda c: c.get("reranker_score") or 0, reverse=True)
    ranked = [c for c in ordered if (c.get("reranker_score") or 0) >= min_score][:top_k]
    return ranked or ordered[:keep_if_empty]


@dataclass
class Context:
    text: str                     # all blocks (confidential ones labelled)
    clauses: list[str]            # clause ids in the context, in rank order
    public_text: str              # only blocks of public chunks
    public_clauses: list[str]
    confidential_clauses: list[str] = field(default_factory=list)  # ids that appear ONLY in confidential chunks
    chars: int = 0
    blocks: int = 0


CONFIDENTIAL_LABEL = " (CONFIDENTIAL – internal use only: do not quote or cite to the customer)"
EMPTY = "(no relevant policy text found)"


def build_context(ranked: list[dict], budget: int) -> Context:
    """Expand to parent sections, de-duplicate, keep within budget, tag every block with its clause ID.
    Chunks with confidential=true are labelled and left out of public_text."""
    blocks: list[tuple[str, bool]] = []
    seen_sections, clauses, public_clauses, conf_clauses, used = set(), [], [], [], 0
    for c in ranked:
        confidential = c.get("confidential") is True
        block = f"[Clause {c['clause_id']}] {c['title']}{CONFIDENTIAL_LABEL if confidential else ''}\n{c['content']}"
        parent = c.get("parent_content")
        section_key = (c.get("doc_id") or c.get("source"), c.get("section"))
        if parent and section_key not in seen_sections and used + len(parent) < budget * 0.6:
            block += f"\n(Section context – {c.get('section')}):\n{parent}"
            seen_sections.add(section_key)
        if used + len(block) > budget:
            break
        blocks.append((block, confidential))
        if c["clause_id"] not in clauses:  # a split clause can contribute several chunks
            clauses.append(c["clause_id"])
        target = conf_clauses if confidential else public_clauses
        if c["clause_id"] not in target:
            target.append(c["clause_id"])
        used += len(block)
    sep = "\n\n---\n\n"
    public_blocks = [b for b, conf in blocks if not conf]
    return Context(
        text=sep.join(b for b, _ in blocks) if blocks else EMPTY,
        clauses=clauses,
        public_text=sep.join(public_blocks) if public_blocks else EMPTY,
        public_clauses=public_clauses,
        confidential_clauses=[c for c in conf_clauses if c not in public_clauses],
        chars=used,
        blocks=len(blocks),
    )
