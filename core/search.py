"""Azure AI Search: hybrid (BM25 + vector, fused with RRF) + semantic ranker.

Access control (confidential documents) is enforced here, in code:
- access="public"            filter `confidential ne true` (security trimming) + post-filter in code
- access="confidential_only" only confidential chunks (used by the chat's score-only probe)
- access="all"               no filter (internal use: claims workflow)
"""
import logging
import time
from functools import lru_cache
from typing import Literal

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizedQuery

from core.config import get_settings
from core.llm import embedder

log = logging.getLogger(__name__)

SearchMode = Literal["keyword", "vector", "hybrid", "hybrid_semantic"]
Access = Literal["public", "confidential_only", "all"]

SELECT_FIELDS = ["id", "clause_id", "title", "section", "content", "parent_content", "source", "policy_type"]
# added by the upload pipeline (ingest/indexer.py); selected only once the index has them
EXTRA_FIELDS = ["doc_id", "chunk_type", "chunk_index", "page", "image_refs", "confidential"]


@lru_cache
def search_client() -> SearchClient:
    s = get_settings()
    if not s.azure_search_endpoint or not s.azure_search_key:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY must be set in .env")
    return SearchClient(s.azure_search_endpoint, s.azure_search_index, AzureKeyCredential(s.azure_search_key))


@lru_cache
def select_fields() -> list[str]:
    """SELECT_FIELDS plus the extra fields that exist in the index (an older index has none of them)."""
    s = get_settings()
    try:
        index = SearchIndexClient(s.azure_search_endpoint, AzureKeyCredential(s.azure_search_key)).get_index(
            s.azure_search_index)
        names = {f.name for f in index.fields}
    except Exception:  # noqa: BLE001 - no index yet / no permission: fall back to the base fields
        return list(SELECT_FIELDS)
    return SELECT_FIELDS + [f for f in EXTRA_FIELDS if f in names]


@lru_cache(maxsize=512)
def embed_query(query: str) -> tuple[float, ...]:
    """Query embedding, cached: the chat's public search and confidential probe embed the same sub-queries."""
    return tuple(embedder().embed_query(query))


def _odata(value: str) -> str:
    return value.replace("'", "''")


_field_checked_at = 0.0


def _has_confidential_field() -> bool:
    """True if the index has the confidential field. A cached "no" is re-checked at most once a minute, so a
    long-running process (API, MCP server) picks the field up after another process added it."""
    global _field_checked_at
    if "confidential" in select_fields():
        return True
    if time.monotonic() - _field_checked_at > 60:
        _field_checked_at = time.monotonic()
        select_fields.cache_clear()
        return "confidential" in select_fields()
    return False


def access_filter(access: Access) -> str | None:
    """OData filter for an access level. `ne true` also matches chunks indexed before the field existed (null)."""
    if access == "all":
        return None
    if not _has_confidential_field():  # index has no confidential field yet: nothing is confidential
        return None if access == "public" else "false"
    return "confidential ne true" if access == "public" else "confidential eq true"


def type_filter(policy_type: str | list[str] | None) -> str | None:
    """policy_type eq 'home', or search.in(...) for several types (e.g. ['home', 'general'])."""
    if not policy_type:
        return None
    if isinstance(policy_type, str):
        return f"policy_type eq '{_odata(policy_type)}'"
    types = [t for t in policy_type if t and "," not in t]
    return f"search.in(policy_type, '{_odata(','.join(types))}', ',')" if types else None


def _combine(*filters: str | None) -> str | None:
    parts = [f"({f})" for f in filters if f]
    return " and ".join(parts) or None


def search_policy(
    query: str,
    policy_type: str | list[str] | None = None,
    mode: SearchMode = "hybrid_semantic",
    top: int | None = None,
    access: Access = "public",
    fields: list[str] | None = None,
) -> list[dict]:
    """Run one query. In hybrid mode Azure fuses BM25 and vector results with Reciprocal Rank Fusion;
    with the semantic ranker, the top 50 fused results are re-scored (@search.reranker_score, 0-4).

    access: "public" (default) never returns confidential chunks; see the module docstring."""
    s = get_settings()
    top = top or s.retrieval_candidates
    access_expr = access_filter(access)
    if access_expr == "false":
        return []
    available = select_fields()
    fields = [f for f in (fields or available) if f in available]
    if access != "all" and "confidential" in available and "confidential" not in fields:
        fields.append("confidential")  # needed for the post-filter
    kwargs: dict = {"top": top, "select": fields}

    if flt := _combine(type_filter(policy_type), access_expr):
        kwargs["filter"] = flt

    if mode in ("vector", "hybrid", "hybrid_semantic"):
        kwargs["vector_queries"] = [
            VectorizedQuery(vector=list(embed_query(query)), k_nearest_neighbors=s.retrieval_candidates,
                            fields="content_vector")
        ]

    search_text = None if mode == "vector" else query

    if mode == "hybrid_semantic":
        kwargs["query_type"] = "semantic"
        kwargs["semantic_configuration_name"] = s.semantic_config

    results = search_client().search(search_text=search_text, **kwargs)
    out = []
    for r in results:
        out.append({
            **{k: r.get(k) for k in fields},
            "score": r.get("@search.score"),
            "reranker_score": r.get("@search.reranker_score"),
        })
    return _post_filter(out, access)


def _post_filter(hits: list[dict], access: Access) -> list[dict]:
    """Layer 2: never trust the filter alone (typo, stale index) – drop what this access level must not see."""
    if access == "public":
        kept = [h for h in hits if h.get("confidential") is not True]
        if len(kept) != len(hits):
            log.warning("Post-filter dropped %d confidential chunk(s) from a public search", len(hits) - len(kept))
        return kept
    if access == "confidential_only":
        return [h for h in hits if h.get("confidential") is True]
    return hits


def get_clause(clause_id: str, policy_type: str | None = None, access: Access = "public") -> dict | None:
    """Full text of one clause. A clause split into several chunks is joined in order; figure chunks of the
    clause are appended after the text. If several documents have this clause id, the first one is used."""
    access_expr = access_filter(access)
    if access_expr == "false":
        return None
    flt = _combine(f"clause_id eq '{_odata(clause_id)}'",
                   f"policy_type eq '{_odata(policy_type)}'" if policy_type else None, access_expr)
    fields = select_fields()
    hits = [{k: r.get(k) for k in fields} for r in search_client().search(search_text="*", filter=flt, top=50,
                                                                           select=fields)]
    hits = _post_filter(hits, access)
    if not hits:
        return None
    doc_key = hits[0].get("doc_id") or hits[0]["source"]
    parts = sorted((h for h in hits if (h.get("doc_id") or h["source"]) == doc_key),
                   key=lambda h: (h.get("chunk_type") == "figure", h.get("chunk_index") or 0))
    clause = dict(parts[0])
    clause["content"] = "\n\n".join(h["content"] for h in parts)
    return clause
