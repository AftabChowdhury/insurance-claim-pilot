"""Azure AI Search: hybrid (BM25 + vector, fused with RRF) + semantic ranker."""
from functools import lru_cache
from typing import Literal

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizedQuery

from core.config import get_settings
from core.llm import embedder

SearchMode = Literal["keyword", "vector", "hybrid", "hybrid_semantic"]

SELECT_FIELDS = ["id", "clause_id", "title", "section", "content", "parent_content", "source", "policy_type"]
# added by the upload pipeline (ingest/indexer.py); selected only once the index has them
EXTRA_FIELDS = ["doc_id", "chunk_type", "chunk_index", "page", "image_refs"]


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


def search_policy(
    query: str,
    policy_type: str | None = None,
    mode: SearchMode = "hybrid_semantic",
    top: int | None = None,
) -> list[dict]:
    """Run one query. In hybrid mode Azure fuses BM25 and vector results with Reciprocal Rank Fusion;
    with the semantic ranker, the top 50 fused results are re-scored (@search.reranker_score, 0-4)."""
    s = get_settings()
    top = top or s.retrieval_candidates
    fields = select_fields()
    kwargs: dict = {"top": top, "select": fields}

    if policy_type:
        kwargs["filter"] = f"policy_type eq '{policy_type}'"

    if mode in ("vector", "hybrid", "hybrid_semantic"):
        vector = embedder().embed_query(query)
        kwargs["vector_queries"] = [
            VectorizedQuery(vector=vector, k_nearest_neighbors=s.retrieval_candidates, fields="content_vector")
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
    return out


def get_clause(clause_id: str, policy_type: str | None = None) -> dict | None:
    """Full text of one clause. A clause split into several chunks is joined in order; figure chunks of the
    clause are appended after the text. If several documents have this clause id, the first one is used."""
    flt = f"clause_id eq '{clause_id.replace(chr(39), chr(39) * 2)}'"
    if policy_type:
        flt += f" and policy_type eq '{policy_type}'"
    fields = select_fields()
    hits = [{k: r.get(k) for k in fields} for r in search_client().search(search_text="*", filter=flt, top=50,
                                                                           select=fields)]
    if not hits:
        return None
    doc_key = hits[0].get("doc_id") or hits[0]["source"]
    parts = sorted((h for h in hits if (h.get("doc_id") or h["source"]) == doc_key),
                   key=lambda h: (h.get("chunk_type") == "figure", h.get("chunk_index") or 0))
    clause = dict(parts[0])
    clause["content"] = "\n\n".join(h["content"] for h in parts)
    return clause
