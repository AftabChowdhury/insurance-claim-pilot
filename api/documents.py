"""Save uploaded files and extract their text."""
import logging
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from core.config import get_settings

log = logging.getLogger(__name__)

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".docx"}
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10


def _safe_name(name: str) -> str:
    name = Path(name).name  # strip any path components
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "file"


def extract_text(path: Path) -> tuple[str | None, str]:
    ext = path.suffix.lower()
    try:
        if ext in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace"), "text"
        if ext == ".pdf":
            import pymupdf4llm
            text = pymupdf4llm.to_markdown(str(path))
            # Scanned PDFs have no text layer; they need OCR (Azure Document Intelligence) – TODO
            return (text, "pdf") if text.strip() else (None, "ocr_pending")
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs), "docx"
        if ext in (".png", ".jpg", ".jpeg"):
            # TODO: OCR / vision model (Azure Document Intelligence or a multimodal LLM)
            return None, "ocr_pending"
    except Exception as e:  # noqa: BLE001
        log.warning("Text extraction failed for %s: %s", path.name, e)
        return None, "failed"
    return None, "unsupported"


async def save_uploads(claim_id: str, files: list[UploadFile]) -> list[dict]:
    if not files:
        raise HTTPException(422, "Attach at least one document")
    if len(files) > MAX_FILES:
        raise HTTPException(422, f"At most {MAX_FILES} documents per claim")

    folder = get_settings().upload_dir / claim_id
    folder.mkdir(parents=True, exist_ok=True)
    docs = []
    for f in files:
        name = _safe_name(f.filename or "file")
        if Path(name).suffix.lower() not in ALLOWED:
            raise HTTPException(422, f"{name}: file type not allowed ({', '.join(sorted(ALLOWED))})")
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(422, f"{name}: larger than 10 MB")
        path = folder / name
        path.write_bytes(data)
        text, extraction = extract_text(path)
        docs.append({"name": name, "path": str(path), "size": len(data), "extraction": extraction, "text": text})
    return docs
