"""Claims system of record (SQLite locally; swap for Azure PostgreSQL later)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from core.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    policy_number TEXT NOT NULL,
    loss_type TEXT NOT NULL,
    amount REAL NOT NULL,
    loss_date TEXT,
    description TEXT,
    documents TEXT DEFAULT '[]',
    status TEXT NOT NULL,
    payout REAL,
    decided_by TEXT,
    letter TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS policy_documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
"""

# Indexing state of policy documents, added to existing databases by init_db().
# status: uploaded -> queued -> processing -> indexed | failed ; stage: current step while processing
POLICY_DOC_COLUMNS = {
    "policy_type": "TEXT",       # admin's choice on upload (None = detect from file)
    "stage": "TEXT",
    "error": "TEXT",
    "chunk_count": "INTEGER",
    "figure_count": "INTEGER",
    "chunker_version": "TEXT",   # chunk settings used for the indexed chunks (re-index when they change)
    "indexed_at": "TEXT",
}

# Audit rows that are not about a claim (admin login, policy uploads) use this pseudo claim id.
ADMIN_AUDIT_ID = "ADMIN"

# Prior claims used by the fraud agent demo (policy AU-20457 has a suspicious history).
SEED_CLAIMS = [
    ("CLM-0901", "AU-20457", "theft", 4200, "2025-06-02", "Stereo and laptop stolen from vehicle", "APPROVED", 3700),
    ("CLM-0955", "AU-20457", "other", 2600, "2025-11-19", "Vandalism – keyed paint", "APPROVED", 1600),
    ("CLM-1003", "AU-20457", "other", 7800, "2026-04-14", "Hit-and-run while parked", "APPROVED", 6800),
    ("CLM-0870", "HO-55821", "water_sudden", 1900, "2024-08-11", "Washing machine hose burst", "APPROVED", 1400),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        existing = {r["name"] for r in c.execute("PRAGMA table_info(policy_documents)")}
        for col, typ in POLICY_DOC_COLUMNS.items():
            if col not in existing:
                c.execute(f"ALTER TABLE policy_documents ADD COLUMN {col} {typ}")
        if c.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0:
            for cid, pol, lt, amt, dt, desc, st, pay in SEED_CLAIMS:
                c.execute(
                    "INSERT INTO claims (id, policy_number, loss_type, amount, loss_date, description, status, payout,"
                    " decided_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, pol, lt, amt, dt, desc, st, pay, "seed", dt + "T00:00:00+00:00", dt + "T00:00:00+00:00"),
                )


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["documents"] = json.loads(d.get("documents") or "[]")
    return d


def create_claim(claim: dict) -> dict:
    ts = now()
    with connect() as c:
        c.execute(
            "INSERT INTO claims (id, policy_number, loss_type, amount, loss_date, description, documents, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (claim["id"], claim["policy_number"], claim["loss_type"], claim["amount"], claim.get("loss_date"),
             claim.get("description"), json.dumps(claim.get("documents", [])), "RECEIVED", ts, ts),
        )
    add_audit(claim["id"], "customer", "submitted", f"{len(claim.get('documents', []))} document(s)")
    return get_claim(claim["id"])


def get_claim(claim_id: str) -> dict | None:
    with connect() as c:
        return _row(c.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone())


def list_claims(status: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM claims", []
    if status:
        q += " WHERE status = ?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [_row(r) for r in c.execute(q, args).fetchall()]


def claim_history(policy_number: str, exclude_id: str | None = None) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, loss_type, amount, loss_date, status, payout FROM claims WHERE policy_number = ? AND id != ?"
            " ORDER BY loss_date DESC",
            (policy_number, exclude_id or ""),
        ).fetchall()
    return [dict(r) for r in rows]


def update_status(claim_id: str, status: str, payout: float | None = None,
                  decided_by: str = "claimpilot", note: str = "") -> dict | None:
    with connect() as c:
        c.execute(
            "UPDATE claims SET status = ?, payout = ?, decided_by = ?, updated_at = ? WHERE id = ?",
            (status, payout, decided_by, now(), claim_id),
        )
    add_audit(claim_id, decided_by, f"status:{status}", note)
    return get_claim(claim_id)


def save_letter(claim_id: str, letter: str) -> None:
    with connect() as c:
        c.execute("UPDATE claims SET letter = ?, updated_at = ? WHERE id = ?", (letter, now(), claim_id))
    add_audit(claim_id, "claimpilot", "letter_saved", f"{len(letter)} chars")


def add_audit(claim_id: str, actor: str, action: str, detail: str = "") -> None:
    with connect() as c:
        c.execute("INSERT INTO audit (claim_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                  (claim_id, now(), actor, action, detail))


def get_audit(claim_id: str) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit WHERE claim_id = ? ORDER BY id", (claim_id,))]


# ---------------------------------------------------------------------------
# Policy documents uploaded from the Admin Panel (files live in settings.policy_upload_dir)
# ---------------------------------------------------------------------------
def add_policy_doc(doc: dict) -> dict:
    with connect() as c:
        c.execute(
            "INSERT INTO policy_documents (id, filename, stored_path, file_type, size_bytes, sha256, status,"
            " uploaded_by, uploaded_at, policy_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc["id"], doc["filename"], doc["stored_path"], doc["file_type"], doc["size_bytes"], doc["sha256"],
             doc.get("status", "uploaded"), doc["uploaded_by"], now(), doc.get("policy_type")),
        )
    return get_policy_doc(doc["id"])


def get_policy_doc(doc_id: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM policy_documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(r) if r else None


def find_policy_doc_by_sha(sha256: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM policy_documents WHERE sha256 = ?", (sha256,)).fetchone()
    return dict(r) if r else None


def list_policy_docs() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM policy_documents ORDER BY uploaded_at DESC, rowid DESC")]


def policy_doc_path(doc: dict):
    """The uploaded file of a policy document. stored_path is absolute and may come from another environment
    (e.g. /app/data/... written inside Docker, with ./data mounted); fall back to the upload folder + name."""
    from pathlib import Path, PurePosixPath, PureWindowsPath

    stored = Path(doc["stored_path"])
    if stored.is_file():
        return stored
    name = PureWindowsPath(doc["stored_path"]).name if "\\" in doc["stored_path"] else \
        PurePosixPath(doc["stored_path"]).name
    return get_settings().policy_upload_dir / name


def update_policy_doc(doc_id: str, **fields) -> None:
    """Update status and/or indexing columns, e.g. update_policy_doc(id, status="processing", stage="chunking")."""
    allowed = {"status", *POLICY_DOC_COLUMNS}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown policy_documents columns: {unknown}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE policy_documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))


def delete_policy_doc(doc_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM policy_documents WHERE id = ?", (doc_id,))
