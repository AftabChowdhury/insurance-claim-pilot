# Feature: Admin policy document upload

An admin logs in to the **Admin Panel** with a static username and password, uploads one or more policy
documents (`.pdf`, `.doc`, `.docx`), sees the list of uploaded documents and can delete them.

This feature covers **upload and file management only**. Uploaded documents are stored and registered, but
they are **not parsed or indexed yet**. Text extraction, chunking, embedding and indexing into Azure AI Search
come in a later branch (see [Next steps](#next-steps)).

## Overview

```
┌──────────────── Streamlit UI (ui/) ────────────────┐        ┌──────────────── FastAPI (api/) ─────────────────┐
│ app.py  st.navigation                              │        │ auth.py   POST   /admin/login                   │
│   ├─ claims_page.py  (existing Claims page)        │  HTTP  │           require_admin (Bearer token check)    │
│   └─ admin_page.py   login · upload · list · delete├───────▶│ admin.py  POST   /admin/policies                │
│ common.py  api() helper                            │        │           GET    /admin/policies                │
└────────────────────────────────────────────────────┘        │           DELETE /admin/policies/{doc_id}       │
                                                              └───────────────┬────────────────┬────────────────┘
                                                                              │                │
                                                     data/policy_uploads/     ▼                ▼  data/claimpilot.db
                                                     <doc_id>_<filename>   files on disk    policy_documents + audit
```

## Files

| File | Role |
|---|---|
| `api/auth.py` | Static login endpoint, token issuing/verification, `require_admin` dependency |
| `api/admin.py` | Upload / list / delete endpoints, file validation |
| `api/files.py` | `safe_name()` – file name sanitising, shared with claim uploads (`api/documents.py`) |
| `api/main.py` | Mounts `auth.router` and `admin.router`; creates `policy_upload_dir` at startup |
| `core/config.py` | Admin and upload settings |
| `core/db.py` | `policy_documents` table + helpers; `ADMIN_AUDIT_ID` |
| `ui/app.py` | Streamlit entrypoint: `st.navigation` with the Claims and Admin pages |
| `ui/admin_page.py` | Admin Panel page |
| `ui/claims_page.py` | Existing claims UI (moved from `ui/app.py`, unchanged behaviour). Later replaced: the review part is now the Admin panel's Claims review tab (`ui/claims_review.py`); customers submit on Submit Claim |
| `ui/common.py` | Shared `api()` HTTP helper |

## Configuration

All settings are read through `core.config.get_settings()` and can be overridden in `.env`.

| Setting (env var) | Default | Meaning |
|---|---|---|
| `ADMIN_USERNAME` | `Admin` | Static admin username |
| `ADMIN_PASSWORD` | `admin` | Static admin password |
| `ADMIN_TOKEN_SECRET` | `change-me` | HMAC key used to sign login tokens – change it outside local demos |
| `ADMIN_TOKEN_TTL_MINUTES` | `480` | Token lifetime (8 hours) |
| `POLICY_UPLOAD_DIR` | `<repo>/data/policy_uploads` | Folder where uploaded files are stored (use an absolute path) |
| `POLICY_MAX_FILES` | `20` | Maximum files per upload request |
| `POLICY_MAX_MB` | `25` | Maximum size per file |

`data/policy_uploads/` is in `.gitignore`.

## Admin authentication

### Login

`POST /admin/login` – form fields `username`, `password`.

- Both values are compared with `secrets.compare_digest` (constant time). The two comparisons are combined
  with `&` rather than `and`, so the password is always checked even when the username is wrong.
- Success → `200`:
  ```json
  {"token": "<token>", "username": "Admin", "expires_at": "2026-11-19T17:00:00+00:00"}
  ```
- Failure → `401 Invalid username or password`.
- Every attempt is written to the `audit` table (`admin_login` / `admin_login_failed`).

### Token format

A stateless, HMAC-signed token (no server-side session store):

```
token   = payload + "." + signature
payload = base64url( {"sub": "<username>", "exp": <unix seconds>} )
signature = hex( HMAC-SHA256(key = ADMIN_TOKEN_SECRET, msg = payload) )
```

`verify_token()` recomputes the signature, compares it with `hmac.compare_digest`, decodes the payload and
checks `exp`. Any malformed, tampered or expired token returns `None`.

### Protecting endpoints

`require_admin` is a FastAPI dependency using `HTTPBearer(auto_error=False)`:

- Reads `Authorization: Bearer <token>`.
- Missing / invalid / expired token → `401 Admin login required` with `WWW-Authenticate: Bearer`.
- Returns the username, which the endpoints use as `uploaded_by` and as the audit actor.

All `/admin/policies` endpoints depend on it. Claim endpoints (`/claims/...`) are not protected.

Because the token is stateless, "log out" only clears it in the UI; a copied token stays valid until it expires.
Changing `ADMIN_TOKEN_SECRET` invalidates all issued tokens.

## Document upload API

All endpoints need `Authorization: Bearer <token>`.

### `POST /admin/policies`

Multipart form with one or more `files` fields.

Request-level checks (whole request fails with `422`):
- no files attached
- more than `POLICY_MAX_FILES` files

Per-file checks, in order (file is **rejected**, other files continue):

| Check | Rejection message |
|---|---|
| Extension is `.pdf`, `.doc` or `.docx` | `file type not allowed (only .doc, .docx, .pdf)` |
| File is not empty | `file is empty` |
| Size ≤ `POLICY_MAX_MB` | `larger than 25 MB` |
| Content starts with the magic bytes of its extension | `content is not a valid .pdf file` |
| SHA-256 not already registered | `duplicate of already uploaded '<filename>'` |

Magic bytes (`SIGNATURES` in `api/admin.py`):

| Extension | Signature | Container |
|---|---|---|
| `.pdf` | `%PDF` | PDF |
| `.docx` | `PK\x03\x04` | ZIP (Office Open XML) |
| `.doc` | `D0 CF 11 E0 A1 B1 1A E1` | OLE2 compound file (Word 97–2003) |

Only the file header is checked; a `.docx` signature matches any ZIP file. Deep validation is left to the
parsing step.

The file is read with `await f.read(max_bytes + 1)`, so an oversized upload is detected without reading it
fully into memory beyond the limit.

Accepted files:
1. `doc_id = uuid4().hex[:12]`
2. Name sanitised with `safe_name()` (drops path components such as `../../`, replaces unusual characters
   with `_`, max 120 chars).
3. Saved to `<POLICY_UPLOAD_DIR>/<doc_id>_<filename>` – the id prefix prevents name collisions.
4. Registered in `policy_documents` with status `uploaded`. If the insert fails, the file is removed again.
5. Audit entry `policy_uploaded`.

Response `200` – one entry per file, in request order (partial success is normal):

```json
[
  {"filename": "home_policy.pdf", "doc_id": "5364e34af28b", "status": "uploaded", "error": null},
  {"filename": "notes.txt", "doc_id": null, "status": "rejected",
   "error": "file type not allowed (only .doc, .docx, .pdf)"}
]
```

### `GET /admin/policies`

Returns all rows of `policy_documents`, newest first.

### `DELETE /admin/policies/{doc_id}`

1. `404 Document not found` if the id is unknown.
2. Deletes the file – only if it is directly inside `POLICY_UPLOAD_DIR` (a safety check; otherwise a warning is
   logged and the file is left alone). A file already missing from disk is not an error.
3. Deletes the registry row and writes an audit entry `policy_deleted`.

Response: `{"doc_id": "<id>", "deleted": true}`.

## Storage

### Files

```
data/policy_uploads/
  5364e34af28b_home_policy.pdf
  268c53f269f6_auto.docx
```

### Registry table (`data/claimpilot.db`)

Created by `db.init_db()` at API startup (`CREATE TABLE IF NOT EXISTS`, so existing databases are upgraded
automatically).

```sql
CREATE TABLE IF NOT EXISTS policy_documents (
    id TEXT PRIMARY KEY,           -- doc_id
    filename TEXT NOT NULL,        -- sanitised original name, shown in the UI
    stored_path TEXT NOT NULL,     -- absolute path of the saved file
    file_type TEXT NOT NULL,       -- pdf | doc | docx
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,   -- duplicate detection
    status TEXT NOT NULL,          -- "uploaded" (indexing statuses added later)
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL      -- UTC ISO-8601
);
```

Helpers in `core/db.py`: `add_policy_doc`, `get_policy_doc`, `find_policy_doc_by_sha`, `list_policy_docs`,
`delete_policy_doc`.

### Audit log

Admin actions reuse the existing `audit` table. Its `claim_id` column is set to `db.ADMIN_AUDIT_ID = "ADMIN"`:

| action | actor | detail |
|---|---|---|
| `admin_login` / `admin_login_failed` | submitted username | – |
| `policy_uploaded` | admin user | `<doc_id> <filename> (<bytes> bytes)` |
| `policy_deleted` | admin user | `<doc_id> <filename>` |

Read them with `db.get_audit("ADMIN")`.

## Admin Panel UI (`ui/admin_page.py`)

The Streamlit app is now multipage (`ui/app.py` uses `st.navigation`):
**Claims** at `/` and **Admin** at `/admin` (http://localhost:8501/admin).

- **Login** – form posting to `/admin/login`. The token and username are kept in `st.session_state`
  (`admin_token`, `admin_user`), i.e. per browser session; a page reload in a new session asks to log in again.
- **Session expiry** – `admin_api()` adds the bearer header to every call; on `401` it clears the session and
  shows the login form with "Your session has expired". `common.api(..., quiet_status=(401,))` returns the raw
  response for those codes instead of showing a generic error.
- **Upload** – `st.file_uploader(type=["pdf", "doc", "docx"], accept_multiple_files=True)` + **Upload** button.
  Per-file success / rejection messages are shown after the upload, and the uploader is cleared by changing its
  widget key.
- **Documents list** – columns File, Type, Size, Uploaded by, Uploaded at (UTC), and a 🗑 button per row.
  **Refresh** reloads the list; an info message is shown when nothing is uploaded.
- **Delete** – 🗑 on a row, or a multiselect + **Delete selected**. Both open a confirmation box
  (**Yes, delete** / **Cancel**); the ids awaiting confirmation are kept in `st.session_state.confirm_delete`.
- **Log out** – clears the session state.

The file type filter in the uploader is a convenience only; the API enforces all checks.

## Testing

There is no automated test suite yet. The feature was verified with:

- **API** (FastAPI `TestClient`, temporary `DB_PATH` / `POLICY_UPLOAD_DIR`): wrong password, missing, invalid,
  tampered and expired tokens → `401`; mixed batch (valid `.pdf`/`.doc`/`.docx` + `.txt`, fake `.pdf`, empty
  file) → valid saved, others rejected with reasons; `../../evil.pdf` saved as `evil.pdf`; duplicate, > 25 MB
  file and 21-file batch rejected; delete removes file and row; unknown id → `404`; audit rows written.
- **UI** (`streamlit.testing.v1.AppTest` against a running API): login failure/success, list rendering, single
  and bulk delete with confirmation, logout, invalid token → back to login; Claims page still loads through
  `st.navigation`.

Manual check:

```powershell
.\run_all.ps1          # or: uv run uvicorn api.main:app --port 8000  +  uv run streamlit run ui/app.py
# open http://localhost:8501/admin and log in with Admin / admin
```

API only (http://localhost:8000/docs, or curl):

```powershell
$t = (Invoke-RestMethod -Method Post http://localhost:8000/admin/login -Form @{username='Admin'; password='admin'}).token
Invoke-RestMethod http://localhost:8000/admin/policies -Headers @{Authorization="Bearer $t"}
```

## Limitations

- Static credentials and a single admin role – demo only, not production authentication.
- Stateless tokens cannot be revoked individually; logout is client-side.
- Claim endpoints remain unauthenticated.
- Header-only content check; no antivirus / macro scanning of Word files.
- Files are stored on the local disk of the API host (no blob storage); in Docker, mount `data/` as a volume to
  keep uploads.
- Uploaded documents are not yet used by retrieval, the claims workflow or the policy chat.

## Next steps

For the indexing branch:

- Process documents with `status = 'uploaded'` from `stored_path`: PDF / `.doc` / `.docx` → markdown → clause
  chunks (same shape as `ingest/ingest_policies.py`) → embeddings → Azure AI Search.
- Add statuses such as `indexing` / `indexed` / `failed` (plus chunk count and error) to `policy_documents`
  and show them in the Admin list.
- Extend `DELETE /admin/policies/{doc_id}` to remove the document's chunks from the search index.
- Make chunk ids unique per document (today `id = f"{policy_type}-{clause_id}"`, so two policies of the same type
  would overwrite each other).
