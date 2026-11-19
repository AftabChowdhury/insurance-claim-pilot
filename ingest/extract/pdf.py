"""PDF extraction with pymupdf4llm's layout engine (features/SAVE_IN_VECTOR_DB.md §1.2).

- Text pages: markdown per page with headings, lists, tables and pictures in reading order. Pictures include
  vector drawings (flowcharts drawn with shapes), rendered as images; text inside them becomes hint_text.
- Page headers/footers are dropped by the layout engine.
- Scanned pages (no text layer) become one 'page_scan' figure each; figures.py OCRs them with the vision model.
"""
import logging
import tempfile
from pathlib import Path

import pymupdf
import pymupdf4llm

from .images import store_image
from .markdown import parse_markdown
from .model import Block, ExtractedDocument, ExtractionError

log = logging.getLogger(__name__)

RENDER_DPI = 200
MIN_TEXT_CHARS = 30


def _is_scanned(page: pymupdf.Page) -> bool:
    return len(page.get_text("text").strip()) < MIN_TEXT_CHARS and bool(page.get_images(full=False))


def extract_pdf(path: Path, figures_dir: Path) -> ExtractedDocument:
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ExtractionError(f"cannot open PDF: {e}") from e
    if doc.needs_pass:
        raise ExtractionError("PDF is password protected")

    scanned = [i for i, page in enumerate(doc) if _is_scanned(page)]
    text_pages = [i for i in range(doc.page_count) if i not in scanned]
    per_page: dict[int, list[Block]] = {}
    warnings: list[str] = []

    if text_pages:
        with tempfile.TemporaryDirectory(prefix="claimpilot_pdf_") as tmp:
            def on_image(ref: str) -> Block | None:
                file = Path(ref)
                if not file.is_file():
                    return None
                info = store_image(file.read_bytes(), figures_dir)
                return Block("figure", source="image", **info) if info else None

            chunks = pymupdf4llm.to_markdown(
                doc, pages=text_pages, page_chunks=True, write_images=True, image_path=tmp, dpi=RENDER_DPI,
                header=False, footer=False, use_ocr=False, show_progress=False,
            )
            for chunk in chunks:
                meta = chunk.get("metadata", {})
                page_no = meta.get("page_number") or meta.get("page")
                per_page[page_no] = parse_markdown(chunk["text"], page=page_no, on_image=on_image)

    for i in scanned:
        pix = doc[i].get_pixmap(dpi=RENDER_DPI)
        info = store_image(pix.tobytes("png"), figures_dir)
        if info:
            per_page[i + 1] = [Block("figure", source="page_scan", page=i + 1, **info)]
        else:
            warnings.append(f"page {i + 1}: scanned page could not be rendered")
    if scanned:
        log.info("%s: %d scanned page(s) need OCR", path.name, len(scanned))

    blocks = [b for page_no in sorted(per_page) for b in per_page[page_no]]
    title = (doc.metadata or {}).get("title") or ""
    page_count = doc.page_count
    doc.close()
    return ExtractedDocument(source=path.name, file_type="pdf", blocks=blocks, title=title,
                             page_count=page_count, warnings=warnings)
