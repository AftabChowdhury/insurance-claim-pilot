"""Policy indexing pipeline: extract -> describe figures -> chunk -> embed -> Azure AI Search (PLAN.md §1, §7).

    await index_policy_file(path, doc_id, work_dir)   # any file (used by ingest_policies for data/policies/)
    await process_policy_doc(doc_id)                  # an admin-uploaded document; keeps its DB status current
    documents_to_index()                              # which uploads need (re)indexing -> resync

In the API, api/indexing.py runs process_policy_doc in a background worker: automatically after every upload,
and a resync on startup. The CLI below does the same without the API:

    uv run python -m ingest.pipeline --pending          # resync: everything new, failed, outdated or missing
    uv run python -m ingest.pipeline --doc-id <id>      # force one document (repeatable)
    uv run python -m ingest.pipeline --all              # force every uploaded document
"""
import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

from core import db
from core.config import get_settings
from ingest.chunking import ChunkSettings, chunk_document
from ingest.extract import ExtractedDocument, extract
from ingest.figures import describe_figures
from ingest.indexer import delete_doc_chunks, doc_chunk_count, ensure_index, index_chunks

log = logging.getLogger(__name__)

# statuses in policy_documents
UPLOADED, QUEUED, PROCESSING, INDEXED, FAILED = "uploaded", "queued", "processing", "indexed", "failed"


async def index_policy_file(path: Path, doc_id: str, work_dir: Path, *, source: str | None = None,
                            policy_type: str | None = None, reextract: bool = False,
                            describe: bool | None = None, on_stage: Callable[[str], None] | None = None) -> dict:
    """Run all steps for one file. Reuses work_dir/extracted.json (and the vision cache) unless reextract."""
    stage = on_stage or (lambda _s: None)
    extracted = work_dir / "extracted.json"
    if extracted.is_file() and not reextract:
        doc = ExtractedDocument.load(extracted)
    else:
        stage("extracting")
        work_dir.mkdir(parents=True, exist_ok=True)
        doc = await asyncio.to_thread(extract, path, work_dir)  # CPU-bound: keep the event loop free
        stage("describing_images")
        await describe_figures(doc, work_dir / "figures.json", enabled=describe)
        doc.save(extracted)

    stage("chunking")
    chunks = chunk_document(doc, doc_id, policy_type, source)
    if not chunks:
        raise ValueError(f"{path.name}: no text found to index")
    stats = await asyncio.to_thread(index_chunks, chunks, doc_id, stage)

    preview = [{k: v for k, v in c.items() if k != "content_vector"} for c in chunks]
    (work_dir / "chunks.json").write_text(json.dumps(preview, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "doc_id": doc_id, "chunks": len(chunks), "policy_type": chunks[0]["policy_type"],
        "figures": sum(not f.decorative for f in doc.figures),
        "figure_chunks": sum(c["chunk_type"] == "figure" for c in chunks),
        "chunker_version": chunks[0]["chunker_version"], "warnings": doc.warnings, **stats,
    }


async def process_policy_doc(doc_id: str, policy_type: str | None = None, reextract: bool = False,
                             actor: str = "system") -> dict | None:
    """Index an admin-uploaded document and keep its status in policy_documents up to date.

    Returns None if the document was deleted before or while it was processed (its chunks are removed)."""
    record = db.get_policy_doc(doc_id)
    if not record:
        return None
    work_dir = get_settings().policy_upload_dir / doc_id
    db.update_policy_doc(doc_id, status=PROCESSING, stage="starting", error=None)

    def on_stage(name: str) -> None:
        db.update_policy_doc(doc_id, stage=name)

    try:
        result = await index_policy_file(
            db.policy_doc_path(record), doc_id, work_dir, source=record["filename"],
            policy_type=policy_type or record.get("policy_type"), reextract=reextract, on_stage=on_stage)
    except Exception as e:
        if db.get_policy_doc(doc_id):
            db.update_policy_doc(doc_id, status=FAILED, stage=None, error=str(e)[:1000])
            db.add_audit(db.ADMIN_AUDIT_ID, actor, "policy_index_failed", f"{doc_id} {record['filename']}: {e}")
        log.exception("Indexing %s failed", doc_id)
        raise

    if not db.get_policy_doc(doc_id):  # deleted while we were indexing: do not leave orphan chunks
        await asyncio.to_thread(delete_doc_chunks, doc_id)
        log.info("%s was deleted during indexing; its chunks were removed again", doc_id)
        return None
    db.update_policy_doc(doc_id, status=INDEXED, stage=None, error=None, chunk_count=result["chunks"],
                         figure_count=result["figures"], chunker_version=result["chunker_version"],
                         policy_type=result["policy_type"], indexed_at=db.now())
    db.add_audit(db.ADMIN_AUDIT_ID, actor, "policy_indexed",
                 f"{doc_id} {record['filename']}: {result['chunks']} chunks ({result['figure_chunks']} figure)")
    return result


def documents_to_index(include_failed: bool = True, check_index: bool = True) -> list[tuple[str, str]]:
    """Uploaded documents whose index content is not in sync, as (doc_id, reason).

    - new / queued / interrupted (status uploaded, queued, processing)
    - failed (only if include_failed)
    - outdated: indexed with other chunk settings (CHUNK_* changed)
    - missing: marked indexed but no chunks in the index (e.g. index rebuilt with --recreate)
    """
    current = ChunkSettings.from_settings().version
    todo: list[tuple[str, str]] = []
    docs = db.list_policy_docs()
    if check_index and docs:
        ensure_index()
    for d in reversed(docs):  # oldest first
        status = d["status"]
        if status in (UPLOADED, QUEUED, PROCESSING):
            todo.append((d["id"], "new" if status == UPLOADED else "interrupted"))
        elif status == FAILED:
            if include_failed:
                todo.append((d["id"], "failed before"))
        elif d.get("chunker_version") != current:
            todo.append((d["id"], "chunk settings changed"))
        elif check_index and doc_chunk_count(d["id"]) == 0:
            todo.append((d["id"], "missing in index"))
    return todo


def main() -> None:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m ingest.pipeline")
    ap.add_argument("--doc-id", action="append", help="uploaded document id (repeatable)")
    ap.add_argument("--pending", action="store_true", help="resync: index new, failed, outdated, missing documents")
    ap.add_argument("--all", action="store_true", help="index every uploaded document")
    ap.add_argument("--policy-type", help="home | auto | general (default: upload choice / front matter / name)")
    ap.add_argument("--reextract", action="store_true", help="ignore extracted.json and extract again")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "azure.core.pipeline.policies.http_logging_policy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db.init_db()
    if args.pending:
        todo = documents_to_index()
    elif args.all:
        todo = [(d["id"], "forced") for d in reversed(db.list_policy_docs())]
    else:
        todo = [(i, "forced") for i in args.doc_id or []]
    if not (args.pending or args.all or args.doc_id):
        ap.error("give --pending, --all or --doc-id")
    if not todo:
        print("Everything is in sync.")
        return

    failed = 0
    for doc_id, reason in todo:
        try:
            r = asyncio.run(process_policy_doc(doc_id, args.policy_type, args.reextract, actor="cli"))
            if r is None:
                print(f"SKIP {doc_id}: document no longer exists")
                continue
            print(f"OK   {doc_id} ({reason}): {r['chunks']} chunks ({r['figure_chunks']} figure), "
                  f"policy_type={r['policy_type']}, stale removed={r['deleted_stale']}")
            for w in r["warnings"]:
                print(f"     warning: {w}")
        except Exception as e:  # noqa: BLE001 - report and continue with the next document
            failed += 1
            print(f"FAIL {doc_id} ({reason}): {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
