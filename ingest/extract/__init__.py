"""Policy document extraction: file -> ExtractedDocument (ordered blocks incl. described figures).

    doc = extract(path, work_dir)                       # local only, no LLM calls
    doc = await extract_document(path, work_dir)        # + vision description / OCR, saved as extracted.json

work_dir holds figures/<sha256>.png, figures.json (vision cache) and extracted.json. For admin uploads it is
settings.policy_upload_dir / <doc_id>.
"""
from collections import Counter
from pathlib import Path

from core.config import get_settings

from .markdown import parse_markdown, split_front_matter
from .model import Block, ExtractedDocument, ExtractionError, attach_captions, render_figure

__all__ = ["Block", "ExtractedDocument", "ExtractionError", "extract", "extract_document", "render_figure",
           "SUPPORTED_EXTENSIONS"]

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md"}


def extract(path: Path, work_dir: Path) -> ExtractedDocument:
    ext = path.suffix.lower()
    figures_dir = work_dir / "figures"
    if ext == ".pdf":
        from .pdf import extract_pdf
        doc = extract_pdf(path, figures_dir)
    elif ext == ".docx":
        from .word import extract_docx
        doc = extract_docx(path, figures_dir)
    elif ext == ".doc":
        from .word import extract_doc
        doc = extract_doc(path, figures_dir)
    elif ext == ".md":
        meta, text = split_front_matter(path.read_text(encoding="utf-8"))
        doc = ExtractedDocument(source=path.name, file_type="md", blocks=parse_markdown(text), front_matter=meta)
    else:
        raise ExtractionError(f"unsupported file type {ext}")
    return finalize(doc)


def finalize(doc: ExtractedDocument) -> ExtractedDocument:
    """Shared post-processing: decorative images, captions, title."""
    mark_decorative(doc.blocks, get_settings().image_min_px)
    doc.blocks = attach_captions(doc.blocks)
    for b in doc.blocks:
        if b.kind == "figure":  # placeholder text until the vision step adds a description
            b.text = "" if b.decorative else render_figure(b)
    if not doc.title:
        doc.title = next((b.text for b in doc.blocks if b.kind == "heading" and b.level <= 1), "")
    return doc


def mark_decorative(blocks: list[Block], min_px: int) -> None:
    """Icons (small) and logos/header graphics (same image repeated on several pages) get no vision call/chunk."""
    figures = [b for b in blocks if b.kind == "figure" and b.source == "image"]
    occurrences = Counter(b.sha256 for b in figures)
    pages: dict[str, set] = {}
    for b in figures:
        pages.setdefault(b.sha256, set()).add(b.page)
    for b in figures:
        too_small = bool(b.width and b.height and min(b.width, b.height) < min_px)
        repeated = len(pages[b.sha256] - {None}) >= 2 or occurrences[b.sha256] >= 3
        b.decorative = too_small or repeated


async def extract_document(path: Path, work_dir: Path, describe: bool | None = None) -> ExtractedDocument:
    """Extract, describe figures / OCR scanned pages with the vision model, save work_dir/extracted.json."""
    from ingest.figures import describe_figures

    work_dir.mkdir(parents=True, exist_ok=True)
    doc = extract(path, work_dir)
    await describe_figures(doc, work_dir / "figures.json", enabled=describe)
    doc.save(work_dir / "extracted.json")
    return doc
