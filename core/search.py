"""Azure AI Search: hybrid (BM25 + vector, fused with RRF) + semantic ranker."""
from functools import lru_cache
from typing import Literal

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

from core.config import get_settings
from core.llm import embedder

SearchMode = Literal["keyword", "vector", "hybrid", "hybrid_semantic"]

SELECT_FIELDS = ["id", "clause_id", "title", "section", "content", "parent_content", "source", "policy_type"]


@lru_cache
def search_client() -> SearchClient:
    s = get_settings()
    if not s.azure_search_endpoint or not s.azure_search_key:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY must be set in .env")
    return SearchClient(s.azure_search_endpoint, s.azure_search_index, AzureKeyCredential(s.azure_search_key))


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
    kwargs: dict = {"top": top, "select": SELECT_FIELDS}

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
            **{k: r.get(k) for k in SELECT_FIELDS},
            "score": r.get("@search.score"),
            "reranker_score": r.get("@search.reranker_score"),
        })
    return out


def get_clause(clause_id: str, policy_type: str | None = None) -> dict | None:
    flt = f"clause_id eq '{clause_id}'"
    if policy_type:
        flt += f" and policy_type eq '{policy_type}'"
    for r in search_client().search(search_text="*", filter=flt, top=1, select=SELECT_FIELDS):
        return {k: r.get(k) for k in SELECT_FIELDS}
    return None
