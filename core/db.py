"""Claims system of record (SQLite locally; swap for Azure PostgreSQL later)."""
import hashlib
import hmac
import json
import secrets
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
CREATE TABLE IF NOT EXISTS claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    dhash TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    exif TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (claim_id, name)
);
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT,
    password TEXT NOT NULL,
    policy_number TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added to the claims table of existing databases by init_db().
CLAIM_COLUMNS = {
    "customer_id": "TEXT",       # set for claims submitted by a logged-in customer
    "decision_reason": "TEXT",   # short customer-facing reason of the decision (auto or admin)
}

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
    "confidential": "INTEGER DEFAULT 0",  # 1 = hidden from the policy chat (admin flag)
}

# Audit rows that are not about a claim (admin login, policy uploads) use this pseudo claim id.
ADMIN_AUDIT_ID = "ADMIN"
# Audit rows of the policy chat (restricted / blocked requests).
CHAT_AUDIT_ID = "CHAT"
# Audit rows of customer logins.
CUSTOMER_AUDIT_ID = "CUSTOMER"

# Fictional demo customers (example.com is a reserved domain). Every password is settings.customer_password.
# CUST-1001 / CUST-1002 own the demo policies used by the sample claims (AU-20457 has a suspicious history).
SEED_CUSTOMERS = [
    ("CUST-1001", "Emma Schneider", "Lindenstrasse 12, 10969 Berlin", "emma.schneider@example.com",
     "+49 30 5550 1001", "HO-55821"),
    ("CUST-1002", "Lukas Weber", "Hafenweg 3, 20457 Hamburg", "lukas.weber@example.com",
     "+49 40 5550 1002", "AU-20457"),
    ("CUST-1003", "Sophia Fischer", "Marienplatz 8, 80331 Munich", "sophia.fischer@example.com",
     "+49 89 5550 1003", "HO-61234"),
    ("CUST-1004", "Noah Wagner", "Koenigsallee 21, 40212 Duesseldorf", "noah.wagner@example.com",
     "+49 211 5550 1004", "AU-31877"),
    ("CUST-1005", "Mia Becker", "Zeil 45, 60313 Frankfurt", "mia.becker@example.com",
     "+49 69 5550 1005", "HO-70412"),
    ("CUST-1006", "Ben Hoffmann", "Schlossstrasse 9, 70173 Stuttgart", "ben.hoffmann@example.com",
     "+49 711 5550 1006", "AU-42950"),
    ("CUST-1007", "Hannah Schulz", "Domkloster 2, 50667 Cologne", "hannah.schulz@example.com",
     "+49 221 5550 1007", "HO-83106"),
    ("CUST-1008", "Elias Koch", "Prager Strasse 17, 01069 Dresden", "elias.koch@example.com",
     "+49 351 5550 1008", "AU-58321"),
    ("CUST-1009", "Lea Richter", "Breite Strasse 30, 04109 Leipzig", "lea.richter@example.com",
     "+49 341 5550 1009", "HO-94570"),
    ("CUST-1010", "Paul Klein", "Kroepcke 4, 30159 Hanover", "paul.klein@example.com",
     "+49 511 5550 1010", "AU-67044"),
]
CUSTOMER_PUBLIC = "id, name, address, email, phone, policy_number, created_at"  # never select the password

_PBKDF2_ITERATIONS = 200_000

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


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(calc, digest)


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        for table, columns in (("policy_documents", POLICY_DOC_COLUMNS), ("claims", CLAIM_COLUMNS)):
            existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns.items():
                if col not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        if c.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            ts, pw = now(), get_settings().customer_password
            for cid, name, addr, email, phone, pol in SEED_CUSTOMERS:
                c.execute(
                    "INSERT INTO customers (id, name, address, email, phone, password, policy_number, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (cid, name, addr, email, phone, hash_password(pw), pol, ts),
                )
        # Link claims without an owner (seed claims, claims submitted via the API) to the policy holder.
        c.execute("UPDATE claims SET customer_id = (SELECT id FROM customers WHERE customers.policy_number ="
                  " claims.policy_number) WHERE customer_id IS NULL")
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


def new_claim_id() -> str:
    while True:
        cid = f"CLM-{secrets.randbelow(8000) + 2000}"
        if not get_claim(cid):
            return cid


def create_claim(claim: dict, actor: str = "customer") -> dict:
    ts = now()
    with connect() as c:
        c.execute(
            "INSERT INTO claims (id, policy_number, loss_type, amount, loss_date, description, documents, status,"
            " customer_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (claim["id"], claim["policy_number"], claim["loss_type"], claim["amount"], claim.get("loss_date"),
             claim.get("description"), json.dumps(claim.get("documents", [])), "RECEIVED",
             claim.get("customer_id"), ts, ts),
        )
    add_audit(claim["id"], actor, "submitted", f"{len(claim.get('documents', []))} document(s)")
    return get_claim(claim["id"])


def get_claim(claim_id: str) -> dict | None:
    with connect() as c:
        return _row(c.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone())


def list_claims(status: str | None = None, limit: int = 50, customer_id: str | None = None) -> list[dict]:
    q, where, args = "SELECT * FROM claims", [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if customer_id:
        where.append("customer_id = ?")
        args.append(customer_id)
    if where:
        q += " WHERE " + " AND ".join(where)
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


def set_status(claim_id: str, status: str, actor: str = "claimpilot", note: str = "") -> None:
    """Interim status (CHECKING, AWAITING_REVIEW, FLAGGED_SECURITY) without touching payout / decided_by."""
    with connect() as c:
        c.execute("UPDATE claims SET status = ?, updated_at = ? WHERE id = ?", (status, now(), claim_id))
    add_audit(claim_id, actor, f"status:{status}", note)


def save_decision_reason(claim_id: str, reason: str) -> None:
    with connect() as c:
        c.execute("UPDATE claims SET decision_reason = ?, updated_at = ? WHERE id = ?", (reason, now(), claim_id))


def count_claims_since(customer_id: str, since_iso: str) -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM claims WHERE customer_id = ? AND created_at >= ?",
                         (customer_id, since_iso)).fetchone()[0]


def claims_to_resume() -> list[dict]:
    """Customer claims whose claim check has not finished (startup resync)."""
    with connect() as c:
        rows = c.execute("SELECT * FROM claims WHERE customer_id IS NOT NULL AND status IN ('RECEIVED', 'CHECKING')"
                         " ORDER BY created_at").fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Claim evidence (image fingerprints for duplicate / reuse detection)
# ---------------------------------------------------------------------------
def save_evidence(claim_id: str, name: str, sha256: str, dhash: str, width: int, height: int, exif: dict) -> None:
    with connect() as c:
        # On re-processing (restart) keep the original upload time: reuse detection compares by upload order.
        c.execute("INSERT INTO claim_evidence (claim_id, name, sha256, dhash, width, height, exif, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (claim_id, name) DO UPDATE SET sha256 = excluded.sha256,"
                  " dhash = excluded.dhash, width = excluded.width, height = excluded.height, exif = excluded.exif",
                  (claim_id, name, sha256, dhash, width, height, json.dumps(exif, default=str), now()))


def delete_evidence(claim_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM claim_evidence WHERE claim_id = ?", (claim_id,))


def find_similar_images(sha256: str, dhash: str, exclude_claim_id: str, max_distance: int) -> list[dict]:
    """Images uploaded earlier with other claims that are identical (sha256) or look the same (dHash Hamming
    distance). Only earlier uploads count, so the original claim is not flagged when its photo is reused later."""
    target = int(dhash, 16)
    with connect() as c:
        rows = c.execute("SELECT e.claim_id, e.name, e.sha256, e.dhash, c.customer_id FROM claim_evidence e"
                         " LEFT JOIN claims c ON c.id = e.claim_id WHERE e.claim_id != ? AND e.created_at <="
                         " COALESCE((SELECT MIN(created_at) FROM claim_evidence WHERE claim_id = ?), e.created_at)",
                         (exclude_claim_id, exclude_claim_id)).fetchall()
    out = []
    for r in rows:
        distance = 0 if r["sha256"] == sha256 else bin(target ^ int(r["dhash"], 16)).count("1")
        if distance <= max_distance:
            out.append({"claim_id": r["claim_id"], "name": r["name"], "customer_id": r["customer_id"],
                        "distance": distance, "exact": r["sha256"] == sha256})
    return sorted(out, key=lambda x: x["distance"])


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
            " uploaded_by, uploaded_at, policy_type, confidential) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc["id"], doc["filename"], doc["stored_path"], doc["file_type"], doc["size_bytes"], doc["sha256"],
             doc.get("status", "uploaded"), doc["uploaded_by"], now(), doc.get("policy_type"),
             int(bool(doc.get("confidential")))),
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


def get_chat_audit(limit: int = 100) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit WHERE claim_id = ? ORDER BY id DESC LIMIT ?",
                                           (CHAT_AUDIT_ID, limit))]


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


# ---------------------------------------------------------------------------
# Customers (demo data; the password column holds a PBKDF2 hash and is never returned)
# ---------------------------------------------------------------------------
def list_customers() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(f"SELECT {CUSTOMER_PUBLIC} FROM customers ORDER BY id")]


def get_customer(customer_id: str) -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT {CUSTOMER_PUBLIC} FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(r) if r else None


def authenticate_customer(email: str, password: str) -> dict | None:
    """The customer (without password) if email + password match, else None. Email is case-insensitive."""
    with connect() as c:
        r = c.execute("SELECT id, password FROM customers WHERE email = ?", (email.strip(),)).fetchone()
    if r is None:
        hash_password(password)  # similar response time for unknown emails
        return None
    return get_customer(r["id"]) if verify_password(password, r["password"]) else None
