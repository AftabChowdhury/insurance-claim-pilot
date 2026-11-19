"""Admin Panel API: upload, list, delete, (re)index and resync policy documents; list customers.

Files are stored in settings.policy_upload_dir as <doc_id>_<filename> and registered in the policy_documents
table. Every upload is queued for indexing automatically (api/indexing.py: extract -> chunk -> embed -> Azure
AI Search); work files go to <policy_upload_dir>/<doc_id>/. Status: uploaded -> queued -> processing ->
indexed | failed. Deleting a document also removes its chunks from the index.

A document can be marked confidential (on upload or later with PATCH). Its chunks carry confidential=true in the
index and are never returned to the policy chat; toggling the flag only updates the chunks' metadata.
"""
import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from core import db
from core.config import get_settings
from ingest.indexer import delete_doc_chunks, set_doc_confidential

from .auth import require_admin
from .files import safe_name

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/policies", tags=["admin"])
chat_audit_router = APIRouter(prefix="/admin/chat", tags=["admin"])
customers_router = APIRouter(prefix="/admin/customers", tags=["admin"])
POLICY_TYPES = ("home", "auto", "general")

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
async def upload_policies(request: Request, files: list[UploadFile] = File(...),
                          policy_type: str | None = Form(None), confidential: bool = Form(False),
                          user: str = Depends(require_admin)):
    """Upload one or more policy documents (.pdf, .doc, .docx). Valid files are saved even if others are rejected.
    policy_type (home | auto | general) and confidential apply to all files; empty policy_type = detect from the
    file. Accepted files are queued for indexing automatically."""
    s = get_settings()
    policy_type = (policy_type or "").strip().lower() or None
    if policy_type and policy_type not in POLICY_TYPES:
        raise HTTPException(422, f"policy_type must be one of {', '.join(POLICY_TYPES)}")
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
                               "sha256": sha, "uploaded_by": user, "policy_type": policy_type,
                               "confidential": confidential})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        db.add_audit(db.ADMIN_AUDIT_ID, user, "policy_uploaded",
                     f"{doc_id} {name} ({len(data)} bytes{', confidential' if confidential else ''})")
        status = "uploaded"
        if s.policy_auto_index:
            request.app.state.indexer.enqueue(doc_id, actor=user)
            status = "queued"
        results.append({"filename": name, "doc_id": doc_id, "status": status, "error": None})
    return results


@router.get("")
def list_policies(_: str = Depends(require_admin)):
    return db.list_policy_docs()


@router.get("/indexing")
def indexing_state(request: Request, _: str = Depends(require_admin)):
    """Background worker state: document being processed and queue length."""
    return request.app.state.indexer.state()


@router.post("/resync")
async def resync_policies(request: Request, user: str = Depends(require_admin)):
    """Queue every document that is not in sync with the index: new, failed, interrupted, indexed with old
    chunk settings, or missing from the index."""
    try:
        queued = await request.app.state.indexer.resync(include_failed=True, actor=user)
    except Exception as e:  # noqa: BLE001 - e.g. Azure AI Search not reachable
        raise HTTPException(502, f"Resync failed: {e}") from e
    return {"queued": queued}


@router.post("/{doc_id}/index")
def reindex_policy(doc_id: str, request: Request, reextract: bool = False, user: str = Depends(require_admin)):
    """(Re)index one document. reextract=true also repeats extraction and image description."""
    if not db.get_policy_doc(doc_id):
        raise HTTPException(404, "Document not found")
    queued = request.app.state.indexer.enqueue(doc_id, reextract=reextract, actor=user)
    return {"doc_id": doc_id, "queued": queued}


class ConfidentialUpdate(BaseModel):
    confidential: bool


@router.patch("/{doc_id}")
def set_confidential(doc_id: str, body: ConfidentialUpdate, user: str = Depends(require_admin)):
    """Mark a document as confidential (hidden from the policy chat) or public again. Updates the DB row and the
    chunks already in the index (metadata only, no re-embedding). A document that is still being indexed gets
    the flag when the worker writes its chunks."""
    doc = db.get_policy_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    old = bool(doc.get("confidential"))
    db.update_policy_doc(doc_id, confidential=int(body.confidential))
    chunks_updated, error = 0, None
    if doc["status"] == "indexed":
        try:
            chunks_updated = set_doc_confidential(doc_id, body.confidential)
        except Exception as e:  # noqa: BLE001 - DB is the source of truth; the next resync repairs the index
            log.warning("Could not update the confidential flag of %s in the index: %s", doc_id, e)
            error = f"Saved, but the search index could not be updated yet ({e}). Run Resync to repair."
    db.add_audit(db.ADMIN_AUDIT_ID, user, "policy_confidentiality_changed",
                 f"{doc_id} {doc['filename']}: confidential {old} -> {body.confidential}"
                 f" ({chunks_updated} chunk(s) updated)")
    return {"doc_id": doc_id, "confidential": body.confidential, "chunks_updated": chunks_updated, "error": error}


@router.get("/{doc_id}/chunks")
def policy_chunks(doc_id: str, _: str = Depends(require_admin)):
    """Chunks of the last indexing run (without vectors) for the preview in the Admin panel."""
    if not db.get_policy_doc(doc_id):
        raise HTTPException(404, "Document not found")
    path = _upload_dir() / doc_id / "chunks.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@router.delete("/{doc_id}")
def delete_policy(doc_id: str, user: str = Depends(require_admin)):
    doc = db.get_policy_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    path = db.policy_doc_path(doc).resolve()
    if path.parent == _upload_dir().resolve():  # never delete outside the upload folder
        path.unlink(missing_ok=True)
    else:
        log.warning("Not deleting %s: outside the policy upload folder", path)
    work_dir = _upload_dir() / doc_id  # extraction output: figures, figures.json, extracted.json, chunks.json
    if work_dir.is_dir():
        shutil.rmtree(work_dir, ignore_errors=True)
    removed_chunks = None
    try:
        removed_chunks = delete_doc_chunks(doc_id)
    except Exception as e:  # noqa: BLE001 - the document is still deleted; report the index problem
        log.warning("Could not remove chunks of %s from the search index: %s", doc_id, e)
    db.delete_policy_doc(doc_id)
    db.add_audit(db.ADMIN_AUDIT_ID, user, "policy_deleted",
                 f"{doc_id} {doc['filename']} (index chunks removed: {removed_chunks})")
    return {"doc_id": doc_id, "deleted": True, "chunks_removed": removed_chunks}


@chat_audit_router.get("/audit")
def chat_audit(limit: int = 100, _: str = Depends(require_admin)):
    """Restricted (confidential) and blocked policy-chat requests, newest first."""
    return db.get_chat_audit(min(max(limit, 1), 500))


@customers_router.get("")
def list_customers(_: str = Depends(require_admin)):
    """Customers for the admin list: name, ID, email and phone only (no address, never the password)."""
    return [{k: c[k] for k in ("id", "name", "email", "phone")} for c in db.list_customers()]
