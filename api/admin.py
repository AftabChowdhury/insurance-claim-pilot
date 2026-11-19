"""Admin Panel API: upload, list and delete policy documents.

Files are stored in settings.policy_upload_dir as <doc_id>_<filename> and registered in the
policy_documents table with status "uploaded". Parsing and indexing happen in a later step.
"""
import hashlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from core import db
from core.config import get_settings

from .auth import require_admin
from .files import safe_name

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/policies", tags=["admin"])

# Allowed extensions and the magic bytes their content must start with.
SIGNATURES = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),                        # ZIP container (Office Open XML)
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),   # OLE2 compound file (Word 97-2003)
}


def _upload_dir() -> Path:
    folder = get_settings().policy_upload_dir
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _check(name: str, data: bytes, max_bytes: int) -> str | None:
    """Return a rejection reason, or None if the file is acceptable."""
    ext = Path(name).suffix.lower()
    if ext not in SIGNATURES:
        return f"file type not allowed (only {', '.join(sorted(SIGNATURES))})"
    if not data:
        return "file is empty"
    if len(data) > max_bytes:
        return f"larger than {max_bytes // (1024 * 1024)} MB"
    if not data.startswith(SIGNATURES[ext]):
        return f"content is not a valid {ext} file"
    return None


@router.post("")
async def upload_policies(files: list[UploadFile] = File(...), user: str = Depends(require_admin)):
    """Upload one or more policy documents (.pdf, .doc, .docx). Valid files are saved even if others are rejected."""
    s = get_settings()
    if not files:
        raise HTTPException(422, "Attach at least one document")
    if len(files) > s.policy_max_files:
        raise HTTPException(422, f"At most {s.policy_max_files} documents per upload")

    max_bytes = s.policy_max_mb * 1024 * 1024
    folder = _upload_dir()
    results = []
    for f in files:
        name = safe_name(f.filename or "file")
        data = await f.read(max_bytes + 1)
        error = _check(name, data, max_bytes)
        sha = hashlib.sha256(data).hexdigest()
        if not error and (dup := db.find_policy_doc_by_sha(sha)):
            error = f"duplicate of already uploaded '{dup['filename']}'"
        if error:
            results.append({"filename": name, "doc_id": None, "status": "rejected", "error": error})
            continue

        doc_id = uuid.uuid4().hex[:12]
        path = folder / f"{doc_id}_{name}"
        path.write_bytes(data)
        try:
            db.add_policy_doc({"id": doc_id, "filename": name, "stored_path": str(path),
                               "file_type": Path(name).suffix.lower().lstrip("."), "size_bytes": len(data),
                               "sha256": sha, "uploaded_by": user})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        db.add_audit(db.ADMIN_AUDIT_ID, user, "policy_uploaded", f"{doc_id} {name} ({len(data)} bytes)")
        results.append({"filename": name, "doc_id": doc_id, "status": "uploaded", "error": None})
    return results


@router.get("")
def list_policies(_: str = Depends(require_admin)):
    return db.list_policy_docs()


@router.delete("/{doc_id}")
def delete_policy(doc_id: str, user: str = Depends(require_admin)):
    doc = db.get_policy_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    path = Path(doc["stored_path"]).resolve()
    if path.parent == _upload_dir().resolve():  # never delete outside the upload folder
        path.unlink(missing_ok=True)
    else:
        log.warning("Not deleting %s: outside the policy upload folder", path)
    db.delete_policy_doc(doc_id)
    db.add_audit(db.ADMIN_AUDIT_ID, user, "policy_deleted", f"{doc_id} {doc['filename']}")
    return {"doc_id": doc_id, "deleted": True}
