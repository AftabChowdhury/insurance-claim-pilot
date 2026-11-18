"""Load policy documents into Azure AI Search with hybrid + semantic configuration.

Usage:
    uv run python -m ingest.ingest_policies            # create index if missing, upload chunks
    uv run python -m ingest.ingest_policies --recreate # drop and rebuild the index
    uv run python -m ingest.ingest_policies --dry-run  # only print the chunks

Policy files live in data/policies/ (.md or .pdf). Structure expected:
    ## Section 4 – Covered Perils          -> section (parent)
    ### 4.2 Sudden and accidental water...  -> clause (chunk); ID = "4.2"
Front matter "policy_type: home|auto" (or a filename starting with home_/auto_) sets the policy type.
"""
import argparse
import re
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (HnswAlgorithmConfiguration, SearchableField, SearchField,
                                                   SearchFieldDataType, SearchIndex, SemanticConfiguration,
                                                   SemanticField, SemanticPrioritizedFields, SemanticSearch,
                                                   SimpleField, VectorSearch, VectorSearchProfile)

from core.config import get_settings
from core.llm import embedder

CLAUSE_RE = re.compile(r"^###\s+([A-Z]?-?\d+(?:\.\d+)?)\s+(.*)$")


def load_markdown(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        import pymupdf4llm
        return pymupdf4llm.to_markdown(str(path))
    return path.read_text(encoding="utf-8")


def parse_policy(path: Path) -> list[dict]:
    text = load_markdown(path)
    policy_type = "home" if path.stem.startswith("home") else "auto" if path.stem.startswith("auto") else "general"
    m = re.search(r"^policy_type:\s*(\w+)", text, re.MULTILINE)
    if m:
        policy_type = m.group(1)

    chunks, section, section_lines, current = [], "", [], None
    section_chunks: list[dict] = []

    def close_clause():
        nonlocal current
        if current:
            current["content"] = "\n".join(current.pop("_lines")).strip()
            chunks.append(current)
            section_chunks.append(current)
            current = None

    def close_section():
        close_clause()
        parent = "\n".join(section_lines).strip()
        for c in section_chunks:
            c["parent_content"] = parent[:6000]
        section_chunks.clear()

    for line in text.splitlines():
        if line.startswith("## "):
            close_section()
            section, section_lines = line[3:].strip(), [line]
            continue
        section_lines.append(line)
        cm = CLAUSE_RE.match(line)
        if cm:
            close_clause()
            cid, title = cm.groups()
            current = {
                "id": f"{policy_type}-{cid}".replace(".", "_"),
                "clause_id": cid, "title": f"{cid} {title.strip()}", "section": section,
                "source": path.name, "policy_type": policy_type, "_lines": [line],
            }
        elif current is not None:
            current["_lines"].append(line)
    close_section()
    return chunks


def build_index(name: str, dims: int, semantic_name: str) -> SearchIndex:
    fields = [
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = get_settings()

    files = sorted(p for p in (s.data_dir / "policies").glob("*") if p.suffix.lower() in (".md", ".pdf"))
    chunks = [c for f in files for c in parse_policy(f)]
    print(f"Parsed {len(chunks)} clauses from {len(files)} file(s)")
    for c in chunks:
        print(f"  [{c['policy_type']}] {c['title']}  ({len(c['content'])} chars)")
    if args.dry_run:
        return

    cred = AzureKeyCredential(s.azure_search_key)
    index_client = SearchIndexClient(s.azure_search_endpoint, cred)
    if args.recreate:
        try:
            index_client.delete_index(s.azure_search_index)
            print(f"Deleted index {s.azure_search_index}")
        except ResourceNotFoundError:
            pass
    index_client.create_or_update_index(build_index(s.azure_search_index, s.embedding_dimensions, s.semantic_config))
    print(f"Index {s.azure_search_index} ready (hybrid + semantic config '{s.semantic_config}')")

    vectors = embedder().embed_documents([f"{c['title']}\n{c['content']}" for c in chunks])
    for c, v in zip(chunks, vectors):
        c["content_vector"] = v
    result = SearchClient(s.azure_search_endpoint, s.azure_search_index, cred).upload_documents(chunks)
    ok = sum(1 for r in result if r.succeeded)
    print(f"Uploaded {ok}/{len(chunks)} chunks")


if __name__ == "__main__":
    main()
