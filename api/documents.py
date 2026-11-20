"""Save uploaded files and extract their text. Images are validated and fingerprinted (api/evidence.py)."""
import logging
import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile

from core import db
from core.config import get_settings

from .evidence import EvidenceError, inspect_image, is_image
from .files import safe_name

log = logging.getLogger(__name__)

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".docx"}
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10


def extract_text(path: Path) -> tuple[str | None, str]:
    ext = path.suffix.lower()
    try:
        if ext in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace"), "text"
        if ext == ".pdf":
            import pymupdf4llm
            text = pymupdf4llm.to_markdown(str(path))
            # Scanned PDFs have no text layer: OCR'd by the vision model in the verify_evidence node
            return (text, "pdf") if text.strip() else (None, "ocr_pending")
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs), "docx"
        if ext in (".png", ".jpg", ".jpeg"):
            return None, "image"  # analysed by the vision model in the verify_evidence node
    except Exception as e:  # noqa: BLE001
        log.warning("Text extraction failed for %s: %s", path.name, e)
        return None, "failed"
    return None, "unsupported"


def describe_file(claim_id: str, path: Path, data: bytes) -> dict:
    """Document record for the workflow: extracted text, or for images the validated evidence record."""
    doc = {"name": path.name, "path": str(path), "size": len(data)}
    if is_image(path):
        image = inspect_image(path, data)  # raises EvidenceError
        db.save_evidence(claim_id, path.name, image["sha256"], image["dhash"], image["width"], image["height"],
                         image["exif"])
        return {**doc, "extraction": "image", "text": None, "image": image}
    text, extraction = extract_text(path)
    return {**doc, "extraction": extraction, "text": text}


async def save_uploads(claim_id: str, files: list[UploadFile]) -> list[dict]:
    if not files:
        raise HTTPException(422, "Attach at least one document")
    if len(files) > MAX_FILES:
        raise HTTPException(422, f"At most {MAX_FILES} documents per claim")

    folder = get_settings().upload_dir / claim_id
    folder.mkdir(parents=True, exist_ok=True)
    docs, seen = [], set()
    try:
        for f in files:
            name = safe_name(f.filename or "file")
            if Path(name).suffix.lower() not in ALLOWED:
                raise HTTPException(422, f"{name}: file type not allowed ({', '.join(sorted(ALLOWED))})")
            if name.lower() in seen:
                raise HTTPException(422, f"{name}: uploaded twice")
            seen.add(name.lower())
            data = await f.read()
            if len(data) > MAX_BYTES:
                raise HTTPException(422, f"{name}: larger than 10 MB")
            if not data:
                raise HTTPException(422, f"{name}: file is empty")
            path = folder / name
            path.write_bytes(data)
            try:
                docs.append(describe_file(claim_id, path, data))
            except EvidenceError as e:
                raise HTTPException(422, str(e)) from e
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)  # nothing is stored for a rejected submission
        db.delete_evidence(claim_id)
        raise
    return docs


def load_documents(claim_id: str, names: list[str]) -> list[dict]:
    """Rebuild the document records of a stored claim (e.g. to restart its check after an API restart)."""
    folder = get_settings().upload_dir / claim_id
    docs = []
    for name in names:
        path = folder / name
        if not path.is_file():
            log.warning("Claim %s: stored file %s is missing", claim_id, name)
            continue
        data = path.read_bytes()
        try:
            docs.append(describe_file(claim_id, path, data))
        except EvidenceError as e:
            docs.append({"name": name, "path": str(path), "size": len(data), "extraction": "failed", "text": None,
                         "error": str(e)})
    return docs
