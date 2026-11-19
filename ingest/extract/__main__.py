"""Extract a policy document and print the result (no chunking / indexing yet).

Usage:
    uv run python -m ingest.extract path\\to\\policy.pdf               # with vision (figures + scanned pages)
    uv run python -m ingest.extract path\\to\\policy.docx --no-vision  # local extraction only
    uv run python -m ingest.extract --doc-id 3f2a9c1b7e44              # an admin-uploaded document
    uv run python -m ingest.extract policy.pdf --blocks                # print blocks instead of markdown

Output (figures, figures.json cache, extracted.json) goes to data/policy_uploads/<doc_id>/ for --doc-id,
otherwise to data/policy_uploads/_preview/<file stem>/ (or --out).
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from core import db
from core.config import get_settings

from . import ExtractionError, extract, extract_document


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m ingest.extract")
    ap.add_argument("file", nargs="?", type=Path)
    ap.add_argument("--doc-id", help="id of an admin-uploaded policy document")
    ap.add_argument("--out", type=Path, help="work directory for figures and extracted.json")
    ap.add_argument("--no-vision", action="store_true", help="skip figure description and scanned-page OCR")
    ap.add_argument("--blocks", action="store_true", help="print the block list instead of markdown")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()

    if args.doc_id:
        record = db.get_policy_doc(args.doc_id)
        if not record:
            sys.exit(f"Unknown doc_id {args.doc_id}")
        path, work_dir = Path(record["stored_path"]), s.policy_upload_dir / args.doc_id
    elif args.file:
        path, work_dir = args.file, s.policy_upload_dir / "_preview" / args.file.stem
    else:
        ap.error("give a file or --doc-id")
    work_dir = args.out or work_dir
    if not path.is_file():
        sys.exit(f"File not found: {path}")

    try:
        if args.no_vision:
            work_dir.mkdir(parents=True, exist_ok=True)
            doc = extract(path, work_dir)
            doc.save(work_dir / "extracted.json")
        else:
            doc = asyncio.run(extract_document(path, work_dir))
    except ExtractionError as e:
        sys.exit(f"Extraction failed: {e}")

    if args.blocks:
        for i, b in enumerate(doc.blocks):
            extra = ""
            if b.kind == "figure":
                extra = (f" source={b.source} {b.width}x{b.height} caption={b.caption!r}"
                         f"{' DECORATIVE' if b.decorative else ''}")
            label = f"{b.kind}{b.level if b.kind == 'heading' else ''}"
            first = b.text.splitlines()[0][:90] if b.text else ""
            print(f"{i:3} p{b.page or '-'} {label:9}{extra} | {first}")
    else:
        print(doc.to_markdown())

    figures = doc.figures
    print(f"\n--- {doc.source}: '{doc.title}', {len(doc.blocks)} blocks, {len(figures)} figures "
          f"({sum(b.decorative for b in figures)} decorative, {sum(b.figure is not None for b in figures)} "
          f"described), pages={doc.page_count or '-'}")
    for w in doc.warnings:
        print(f"WARNING: {w}")
    print(f"Saved {work_dir / 'extracted.json'}")


if __name__ == "__main__":
    main()
