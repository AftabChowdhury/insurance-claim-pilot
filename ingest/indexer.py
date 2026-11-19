"""Embedding + Azure AI Search indexing (PLAN.md §6).

- build_index(): index schema (hybrid BM25 + vector + semantic config). New fields are only *added* to the
  existing schema, so create_or_update works on an existing index without a rebuild.
- embed_chunks(): text-embedding-3-large on chunk["embed_text"] in batches, with retry/backoff on 429.
- index_chunks(): idempotent per document: upload the new chunks, then delete chunks of this doc_id that are
  no longer produced (no window in which the document has no chunks).
- delete_doc_chunks(): remove all chunks of a document (admin delete).
"""
import logging
import time
from functools import lru_cache

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from core.config import get_settings
from core.llm import embedder

log = logging.getLogger(__name__)

# fields uploaded to the index (chunk dicts also carry embed_text / tokens, which are not stored)
INDEX_FIELDS = ["id", "clause_id", "title", "section", "content", "parent_content", "source", "policy_type",
                "doc_id", "chunk_type", "chunk_index", "page", "image_refs", "chunker_version", "content_vector"]
UPLOAD_BATCH = 100        # ~3072 floats per chunk: keep requests well below the 16 MB limit
MAX_EMBED_TOKENS = 8000   # text-embedding-3-large input limit is 8191 tokens


class IndexingError(Exception):
    pass


def build_index(name: str, dims: int, semantic_name: str) -> SearchIndex:
    fields = [
        # --- original fields (unchanged, so the existing index can be updated in place) ---
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="clause_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="section", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="parent_content", type=SearchFieldDataType.String),
        SimpleField(name="source", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="policy_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=dims, vector_search_profile_name="hnsw-profile"),
        # --- added for uploaded documents (PLAN.md §6) ---
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="page", type=SearchFieldDataType.Int32),
        SimpleField(name="image_refs", type=SearchFieldDataType.Collection(SearchFieldDataType.String)),
        SimpleField(name="chunker_version", type=SearchFieldDataType.String, filterable=True),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
        profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw")],
    )
    semantic = SemanticSearch(configurations=[SemanticConfiguration(
        name=semantic_name,
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[SemanticField(field_name="content")],
            keywords_fields=[SemanticField(field_name="clause_id"), SemanticField(field_name="section")],
        ),
    )])
    return SearchIndex(name=name, fields=fields, vector_search=vector_search, semantic_search=semantic)


# ----------------------------------------------------------------------------------------------- clients
def _credential() -> AzureKeyCredential:
    s = get_settings()
    if not s.azure_search_endpoint or not s.azure_search_key:
        raise IndexingError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY (admin key) must be set in .env")
    return AzureKeyCredential(s.azure_search_key)


def index_client() -> SearchIndexClient:
    return SearchIndexClient(get_settings().azure_search_endpoint, _credential())


@lru_cache
def writer() -> SearchClient:
    s = get_settings()
    return SearchClient(s.azure_search_endpoint, s.azure_search_index, _credential())


_ensured = False


def ensure_index(recreate: bool = False) -> None:
    """Create the index, or add missing fields to it. Fails if the vector size does not match the embeddings."""
    global _ensured
    if _ensured and not recreate:
        return
    s = get_settings()
    client = index_client()
    if recreate:
        try:
            client.delete_index(s.azure_search_index)
            log.info("Deleted index %s", s.azure_search_index)
        except ResourceNotFoundError:
            pass
    else:
        try:
            existing = client.get_index(s.azure_search_index)
            vec = next((f for f in existing.fields if f.name == "content_vector"), None)
            if vec is not None and vec.vector_search_dimensions != s.embedding_dimensions:
                raise IndexingError(
                    f"Index '{s.azure_search_index}' has {vec.vector_search_dimensions}-dim vectors but "
                    f"EMBEDDING_DIMENSIONS={s.embedding_dimensions}. Rebuild it: "
                    "uv run python -m ingest.ingest_policies --recreate")
        except ResourceNotFoundError:
            pass
    client.create_or_update_index(build_index(s.azure_search_index, s.embedding_dimensions, s.semantic_config))
    _ensured = True
    try:  # let running searches pick up the new fields
        from core.search import select_fields
        select_fields.cache_clear()
    except ImportError:
        pass


# ----------------------------------------------------------------------------------------------- embedding
def _truncate(text: str) -> str:
    from ingest.chunking import _encoder
    enc = _encoder()
    if enc is None:
        return text[:MAX_EMBED_TOKENS * 4]
    tokens = enc.encode(text, disallowed_special=())
    return text if len(tokens) <= MAX_EMBED_TOKENS else enc.decode(tokens[:MAX_EMBED_TOKENS])


def _embed_batch(texts: list[str], attempts: int = 6) -> list[list[float]]:
    import openai
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return embedder().embed_documents(texts)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                openai.InternalServerError) as e:
            if attempt == attempts:
                raise IndexingError(f"embedding failed after {attempts} attempts: {e}") from e
            log.warning("Embedding attempt %d failed (%s); retrying in %.0fs", attempt, type(e).__name__, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return []


def embed_chunks(chunks: list[dict]) -> None:
    """Set chunk['content_vector'] from chunk['embed_text'] (contextual header + content)."""
    s = get_settings()
    texts = [_truncate(c.get("embed_text") or f"{c['title']}\n{c['content']}") for c in chunks]
    for start in range(0, len(texts), s.embed_batch_size):
        vectors = _embed_batch(texts[start:start + s.embed_batch_size])
        for c, v in zip(chunks[start:start + s.embed_batch_size], vectors):
            if len(v) != s.embedding_dimensions:
                raise IndexingError(f"embedding has {len(v)} dimensions, expected {s.embedding_dimensions}")
            c["content_vector"] = v


# ----------------------------------------------------------------------------------------------- upload / delete
def _odata(value: str) -> str:
    return value.replace("'", "''")


def chunk_ids(filter_expr: str) -> set[str]:
    ids: set[str] = set()
    skip = 0
    while True:
        page = list(writer().search(search_text="*", filter=filter_expr, select=["id"], top=1000, skip=skip))
        ids.update(r["id"] for r in page)
        if len(page) < 1000:
            return ids
        skip += 1000


def _delete_ids(ids: set[str]) -> int:
    ids_list = sorted(ids)
    for start in range(0, len(ids_list), 1000):
        writer().delete_documents([{"id": i} for i in ids_list[start:start + 1000]])
    return len(ids_list)


def index_chunks(chunks: list[dict], doc_id: str, on_stage=None) -> dict:
    """Embed and upload the chunks of one document; remove its stale chunks. Returns counts."""
    stage = on_stage or (lambda _s: None)
    ensure_index()
    if any(c["doc_id"] != doc_id for c in chunks):
        raise IndexingError("all chunks must belong to doc_id " + doc_id)
    old_ids = chunk_ids(f"doc_id eq '{_odata(doc_id)}'")
    stage("embedding")
    embed_chunks(chunks)
    stage("indexing")

    docs = [{k: c.get(k) for k in INDEX_FIELDS} for c in chunks]
    failed: list[str] = []
    for start in range(0, len(docs), UPLOAD_BATCH):
        for r in writer().merge_or_upload_documents(docs[start:start + UPLOAD_BATCH]):
            if not r.succeeded:
                failed.append(f"{r.key}: {r.error_message}")
    if failed:
        raise IndexingError(f"{len(failed)} chunk(s) failed to upload, e.g. {failed[0]}")

    deleted = _delete_ids(old_ids - {d["id"] for d in docs})
    log.info("Indexed %s: %d chunks uploaded, %d stale removed", doc_id, len(docs), deleted)
    return {"uploaded": len(docs), "deleted_stale": deleted}


def doc_chunk_count(doc_id: str) -> int:
    """Number of chunks of a document currently in the index (0 if the index does not exist)."""
    try:
        results = writer().search(search_text="*", filter=f"doc_id eq '{_odata(doc_id)}'", top=0,
                                  include_total_count=True)
        return results.get_count() or 0
    except ResourceNotFoundError:
        return 0


def delete_doc_chunks(doc_id: str) -> int:
    """Remove every chunk of a document from the index. Returns the number deleted."""
    ensure_index()
    return _delete_ids(chunk_ids(f"doc_id eq '{_odata(doc_id)}'"))


def delete_legacy_chunks() -> int:
    """Chunks written by the old ingest (no doc_id), replaced by the shared pipeline."""
    ensure_index()
    return _delete_ids(chunk_ids("doc_id eq null"))
