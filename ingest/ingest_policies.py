"""Create / rebuild the Azure AI Search index and sync the admin-uploaded policies into it.

Only admin uploads are indexed by default. The built-in policies in data/policies/ are indexed only with --builtin.

Usage:
    uv run python -m ingest.ingest_policies                      # create/update index, sync admin uploads
    uv run python -m ingest.ingest_policies --recreate           # drop and rebuild the index, re-index admin uploads
    uv run python -m ingest.ingest_policies --builtin            # also (re)index every file in data/policies/
    uv run python -m ingest.ingest_policies --builtin --dry-run  # only print the built-in chunks (no upload)

Files: .md, .pdf, .docx, .doc in data/policies/. Structure expected (see ingest/structure.py for the general rules):
    ## Section 4 – Covered Perils          -> section (parent)
    ### 4.2 Sudden and accidental water...  -> clause (chunk); ID = "4.2"
Front matter "policy_type: home|auto" (or a filename starting with home/auto) sets the policy type.
Each file gets doc_id "builtin-<file name>"; work files go to data/policy_uploads/_builtin/.
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from core import db
from core.config import get_settings
from ingest.chunking import chunk_document
from ingest.extract import SUPPORTED_EXTENSIONS, extract
from ingest.indexer import delete_legacy_chunks, ensure_index
from ingest.pipeline import documents_to_index, index_policy_file, process_policy_doc


def builtin_doc_id(path: Path) -> str:
    return "builtin-" + re.sub(r"[^A-Za-z0-9_-]", "_", path.stem)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="delete and rebuild the index first")
    ap.add_argument("--builtin", action="store_true",
                    help="also index the built-in policies in data/policies/ (off by default: admin uploads only)")
    ap.add_argument("--dry-run", action="store_true", help="print the built-in chunks only (with --builtin)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "azure.core.pipeline.policies.http_logging_policy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = get_settings()

    files = sorted(p for p in (s.data_dir / "policies").glob("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS)         if args.builtin else []
    if args.dry_run and not args.builtin:
        ap.error("--dry-run prints built-in chunks; use it with --builtin")
    work_root = s.policy_upload_dir / "_builtin"

    if args.dry_run:
        total = 0
        for f in files:
            work_dir = work_root / f.name.replace(".", "_")
            work_dir.mkdir(parents=True, exist_ok=True)
            chunks = chunk_document(extract(f, work_dir), builtin_doc_id(f))
            total += len(chunks)
            for c in chunks:
                print(f"  [{c['policy_type']}] {c['clause_id']:6} {c['chunk_type']:6} {c['tokens']:4} tok  "
                      f"{c['title']}")
        print(f"{total} chunks from {len(files)} file(s) (dry run, nothing uploaded)")
        return

    ensure_index(recreate=args.recreate)
    print(f"Index {s.azure_search_index} ready (hybrid + semantic config '{s.semantic_config}')")
    if not args.recreate and (n := delete_legacy_chunks()):
        print(f"Removed {n} chunks written by the old ingest")

    if not args.builtin:
        print("Skipping data/policies/ (built-in policies are not indexed; pass --builtin to include them)")
    total = 0
    for f in files:
        result = asyncio.run(index_policy_file(f, builtin_doc_id(f), work_root / f.name.replace(".", "_"),
                                               reextract=True))
        total += result["chunks"]
        print(f"  {f.name}: {result['chunks']} chunks [{result['policy_type']}], "
              f"stale removed={result['deleted_stale']}")
    if files:
        print(f"Uploaded {total} chunks from {len(files)} built-in file(s)")

    # bring admin-uploaded documents back in sync (needed after --recreate; a no-op otherwise)
    db.init_db()
    todo = documents_to_index(include_failed=False)
    for doc_id, reason in todo:
        try:
            r = asyncio.run(process_policy_doc(doc_id, actor="cli"))
            if r:
                print(f"  upload {doc_id} ({reason}): {r['chunks']} chunks")
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"  upload {doc_id} ({reason}) FAILED: {e}")


if __name__ == "__main__":
    main()
