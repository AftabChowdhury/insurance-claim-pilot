# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project goal

**ClaimPilot** is a hackathon AI insurance copilot. It has three user-facing capabilities:

1. **Policy chat** – a user asks questions about their insurance policies in natural language and gets answers
   grounded in (and citing) the actual policy clauses.
2. **Claim document upload** – a user submits a claim with supporting documents (PDF, images such as photos,
   invoices, police reports) and sees the result: coverage decision, payout, fraud signals, a cited decision
   letter, and the live agent timeline.
3. **Admin policy management** – an admin uploads policy documents (PDF and Word `.doc`/`.docx`, which may
   contain images) from an admin panel; they are parsed, chunked, embedded and indexed so both the chat and
   the claims workflow can use them.

The emphasis is on trustworthy answers: advanced RAG with hybrid search + semantic ranking, clause citations,
an LLM evaluator with retry, guardrails (prompt injection, PII, output policy), and human-in-the-loop review
for high-stakes claims.

## Current status vs. goal

| Capability | Status |
|---|---|
| Claim submission + multi-agent decision | Implemented (`POST /claims`, LangGraph in `api/graph/`) |
| Claim documents: PDF (text layer), `.docx`, `.txt`, `.md` | Implemented (`api/documents.py`) |
| Claim documents: images and scanned PDFs | Implemented (testing) – images validated + fingerprinted at upload (`api/evidence.py`), checked in the `verify_evidence` node (`api/graph/evidence.py`: reuse, EXIF, vision model, consistency); scanned PDFs OCR'd by the vision model |
| Policy chat | Implemented – LangGraph in `api/chat/`, `/chat` + `/chat/stream`, Streamlit Policy Chat page; evaluator + retry/regenerate; see `features/POLICY_CHAT.md` |
| Confidential policy documents | Implemented – Admin 🔒 flag (upload + `PATCH /admin/policies/{id}`), index field `confidential`; the chat never returns them (filter + post-filter + score-only probe + leak check); see `features/POLICY_CHAT.md` §1–2 |
| Admin panel for policy upload | Implemented – Streamlit Admin page + `/admin/policies` (upload `.pdf`/`.doc`/`.docx`, list, delete, confidential toggle) storing files in `data/policy_uploads/` |
| Extraction of policy documents (`.pdf`/`.docx`/`.doc`/`.md`, images, workflow diagrams, scanned pages) | Implemented – `ingest/extract/` + `ingest/figures.py` (vision model = chat deployment); CLI `uv run python -m ingest.extract`; see `features/SAVE_IN_VECTOR_DB.md` §1 |
| Chunking of policy documents | Implemented – `ingest/structure.py` + `ingest/chunking.py` (clause-based, token limits); CLI `uv run python -m ingest.chunking` |
| Embedding / indexing of policies | Implemented – `ingest/indexer.py` + `ingest/pipeline.py`; built-in policies only via `ingest_policies --builtin` (default: admin uploads only); admin delete removes chunks |
| Automatic indexing after admin upload + resync | Implemented – background worker `api/indexing.py` (queue, resync on API start), Admin page status / re-index / resync / chunk preview (`features/SAVE_IN_VECTOR_DB.md` §4.3) |
| Customers + customer claim form | Implemented – seeded `customers` table (`core/db.py`, claims linked by policy number), admin Customers tab + `GET /admin/customers`, customer login (email + static password `user`), Submit Claim + My Claims (claim history) pages and `/customer/claims`; customers see a reduced status (security / review states shown as "in review") |
| Claims review (agent timeline + admin review) | Admin panel tab (`ui/claims_review.py`): waiting claims first, evidence tab, Accept / Reject (reason shown to the customer), Release for security-flagged claims; see `features/CLAIM_REVIEW.md` |
| Claim check for customer claims | Implemented (testing) – `POST /customer/claims` starts the workflow (`api/claim_flow.py`, startup resume); automatic approve / deny ≤ `HITL_PAYOUT_THRESHOLD` (5,000 €), admin review otherwise |
| Authentication / admin vs. user roles | Partial – static admin and customer logins (`api/auth.py`, HMAC bearer token with `role` admin/customer); internal `/claims*` endpoints require the admin token; chat endpoints are open (chat users always get `access="public"`) |

## Tech stack

- Python ≥ 3.11, dependency management with **uv** (`uv sync`, `uv run ...`). Windows / PowerShell dev environment.
- **FastAPI** backend (`api/main.py`), **Streamlit** UI (`ui/app.py`).
- **LangGraph** agent workflow with SQLite checkpointer (`data/checkpoints.db`) for pause/resume.
- **OpenAI** by default, **Azure OpenAI** when `AZURE_OPENAI_ENDPOINT` is set (`core/llm.py`).
- **Azure AI Search** for hybrid (BM25 + vector, RRF) + semantic ranker (`core/search.py`).
- **MCP** servers (`mcp_servers/`) for claims, policy search and fraud tools; called via `api/mcp_client.py`
  with local fallback if a server is down.
- SQLite claims DB + audit log (`core/db.py`, `data/claimpilot.db`).
- Document parsing: `pymupdf4llm` (PDF → markdown), `python-docx`.
- Optional Presidio PII masking (`uv sync --extra pii`), regex fallback otherwise.
- LangSmith tracing via `LANGSMITH_*` env vars.

## Repository layout

```
api/                FastAPI app
  main.py           Endpoints: /claims, /claims/{id}, /claims/{id}/events (SSE), /claims/{id}/review,
                    /claims/{id}/letter, /claims/{id}/audit, /loss-types, /graph, /health
  documents.py      Upload validation (types, 10 MB, max 10 files) + text extraction; images via evidence.py
  evidence.py       Claim images, step A: real-image check, sha256 + dHash, EXIF / AI markers, sanitised copy
  claim_flow.py     initial_state + start_check (CHECKING) for a stored claim; resume_pending on API start
  auth.py           Static admin + customer logins (POST /admin/login, /customer/login); require_admin /
                    require_customer bearer-token dependencies (token role admin | customer)
  customer.py       /customer/me, POST|GET /customer/claims, GET /customer/claims/{id} (own claims only; submit
                    starts the claim check, rate limited; customer-facing status + reason + letter once decided)
  admin.py          /admin/policies: upload (.pdf/.doc/.docx), list, delete, resync, re-index, chunk preview,
                    PATCH confidential flag; /admin/chat/audit (restricted / blocked chat requests);
                    GET /admin/customers
  indexing.py       IndexWorker: background queue that indexes uploads; resync on startup
  files.py          safe_name() for uploaded file names
  runner.py         Background graph runs, status (running / awaiting_review / completed / failed)
  mcp_client.py     MCP tool calls with fallback
  graph/            LangGraph workflow
    graph.py        Graph wiring (see diagram in module docstring)
    state.py        ClaimState TypedDict + event() helper for the UI timeline
    evidence.py     verify_evidence node: image reuse / metadata / vision / consistency checks, scanned-PDF OCR
    agents.py       intake, coverage, fraud, writer, evaluator agents + retry routing
    retrieval.py    rewrite_query → hybrid_search → semantic_rank → build_context
    guardrails.py   input (normalised text, injection incl. file names + image text, PII); output policy checks +
                    decision rules (auto approve / deny / need_info vs. admin review)
    human.py        interrupt() for admin review (accept / reject / release), close_claim (status, reason, letter)
    prompts.py, schemas.py
  chat/             Policy-chat LangGraph (see features/POLICY_CHAT.md)
    graph.py        guard_input → rewrite_query → [hybrid_search ∥ confidential_probe] → semantic_rank →
                    build_context → access_check → answer_agent → evaluator_agent (↺ prepare_retry) →
                    guard_output → respond
    state.py        ChatState, turn_input() (per-turn reset; history persists per session thread)
    agents.py       query, retrieval, access control, answer, evaluator, retry and responder nodes
    guardrails.py   input (injection, confidentiality bypass, PII) and output (PII, promises, citations, leak check)
    router.py       /chat, /chat/stream (SSE), /chat/{id}/history, DELETE /chat/{id}; rate limit
core/               Shared: config (pydantic-settings), llm clients, search (access levels), rag (shared
                    merge/rank/context helpers), db (claims, audit, policy docs, customers + seed data),
                    domain (loss types)
ingest/             ingest_policies.py – create / rebuild the index + sync admin uploads; data/policies/ only with
                    --builtin (doc_id builtin-<stem>); by default only admin uploads are indexed
  pipeline.py       extract → describe → chunk → embed → index; process_policy_doc(doc_id) for uploads
  indexer.py        index schema, batched embedding with retry, upsert / delete chunks by doc_id
  extract/          policy file → ExtractedDocument (ordered blocks: heading/paragraph/list/table/figure);
                    pdf.py (pymupdf4llm layout), word.py (.docx walk, .doc via LibreOffice), markdown.py
  figures.py        vision model: figure descriptions (workflow steps), scanned-page OCR, figures.json cache
  structure.py      blocks → sections/clauses (clause_id, parent_content, policy_type)
  chunking.py       clause-based chunks with token limits, figure chunks → index-shaped dicts (+ CLI)
mcp_servers/        claims_server (8001), policy_server (8002), fraud_server (8003)
ui/app.py           Streamlit entrypoint (st.navigation): chat_page.py (default; policy chat, live agent
                    timeline), submit_claim_page.py (customer claim form), my_claims_page.py (customer's claim
                    status + decision letter) and admin_page.py (login; tabs Policy documents (upload/list/delete,
                    confidential toggle, chat audit), Claims review (claims_review.py: agent timeline, adjuster
                    review) and Customers); common.py api() + shared customer login
eval/               retrieval_eval.py + testset.json (keyword vs vector vs hybrid vs hybrid+semantic);
                    chat_eval.py + chat_testset.json (answers, citations, confidential leaks, injection)
data/               policies/, sample_claims/, fraud_watchlist.json, uploads/, policy_uploads/ and SQLite DBs (runtime)
```

## Claim workflow (LangGraph)

```
verify_evidence → guard_input ─(blocked)→ human_review (security: reject | release → intake_agent)
  → intake_agent → rewrite_query → hybrid_search → semantic_rank → build_context
  → [coverage_agent ∥ fraud_agent] → writer_agent → evaluator_agent
      ─(score < EVAL_THRESHOLD, retries left)→ prepare_retry → rewrite_query
  → guard_output ─(amount/payout > HITL_PAYOUT_THRESHOLD, fraud ≥ FRAUD_REVIEW_THRESHOLD, suspicious evidence,
                   violations …)→ human_review (admin accept / reject)
  → close_claim → END
```
The final status comes from code (`guard_output` decision rules, `close_claim`) or the admin, never from an LLM.
Which agent does what, the decision rules and the admin review: `features/CLAIM_REVIEW.md`.

Nodes return partial state updates; append UI timeline entries with `event(node, message, status)`.
The claims workflow searches with `access="all"` because it is internal. Confidential clauses are labelled in the
context, the letter writer only gets `public_context`, and `guard_output` escalates a letter that cites a
confidential clause.

## Policy chat workflow (LangGraph, `api/chat/`)

```
guard_input ─(blocked)→ respond
  → rewrite_query → [hybrid_search (public) ∥ confidential_probe (scores + hashes only)] → semantic_rank
  → build_context → access_check ─(restricted | not_found)→ respond
  → answer_agent → evaluator_agent ─(fail, retries left)→ prepare_retry → rewrite_query | answer_agent
  → guard_output → respond → END
```

Status per answer: `answered | partial | restricted | not_found | blocked | fallback`. Confidentiality is enforced
in code (`core.search` access levels), never by prompting. The chat's LLMs must never receive confidential
chunk text.

## Policy document conventions

Policies are chunked by markdown headings (after PDF → markdown conversion):

- `## Section N – Title` → section (parent context, stored in `parent_content`)
- `### N.M Clause title` → clause chunk, `clause_id = "N.M"`
- `policy_type` from front matter (`policy_type: home|auto`) or filename prefix (`home_`, `auto_`), else `general`.
- `confidential: true` in front matter (built-ins) or the admin flag (uploads) marks a document confidential.

Index fields: `id, clause_id, title, section, content, parent_content, source, policy_type, content_vector`, plus
`doc_id, chunk_type, chunk_index, page, image_refs, chunker_version, confidential` (see `features/SAVE_IN_VECTOR_DB.md`).
Any new search caller must choose an `access` level in `core.search.search_policy` (default `public`).
Answers and letters must cite `clause_id`s. New ingestion paths (admin upload, `.docx`, images) should produce
the same chunk shape so retrieval keeps working.

## Commands

```powershell
uv sync                                              # install deps
copy .env.example .env                               # fill OPENAI_API_KEY, AZURE_SEARCH_KEY
uv run python -m ingest.ingest_policies --recreate   # (re)build the search index, re-index admin uploads
uv run python -m ingest.ingest_policies --builtin    # also index data/policies/ (off by default; --dry-run to preview)
uv run python -m ingest.pipeline --pending           # resync uploads without the API (--doc-id <id> / --all to force)
.\run_all.ps1                                        # start 3 MCP servers + API + UI in separate windows
.\stop_all.ps1                                       # stop them all and close their windows
uv run uvicorn api.main:app --reload --port 8000     # API only (docs: /docs, graph: /graph)
uv run streamlit run ui/app.py                       # UI only (http://localhost:8501)
uv run python -m eval.retrieval_eval                 # retrieval comparison
uv run python -m eval.chat_eval                      # policy chat: answers, citations, confidential leaks (must be 0)
docker compose up --build                            # everything in containers
```

There is no unit test suite yet. Verify changes by running the API/UI, the demo scenarios in
`data/sample_claims/README.md`, and `eval/chat_eval.py` for the policy chat.

## Conventions

- All settings go through `core.config.get_settings()`; add new ones there and to `.env.example`.
  Whenever `.env.example` changes, add the new variables the setup needs to the local `.env` too (in the
  matching section, example defaults, random values for secrets; never overwrite or print existing values).
- Use `core.llm.chat(mini=...)` / `structured(schema)` – never instantiate model clients directly, so the
  OpenAI/Azure switch keeps working. Mini model for guardrails, query rewrite and judging.
- Treat uploaded documents as untrusted: they pass through `guard_input` (injection + PII) before reaching agents.
- Keep MCP tools usable with fallback (`call_tool_or_fallback`) so the demo runs without the MCP servers.
- `PLAN.md` is a temporary, git-ignored working plan for the feature in progress. When a feature is finished,
  document it in `features/<FEATURE>.md` (e.g. `features/DOCUMENT_UPLOAD.md`, `features/SAVE_IN_VECTOR_DB.md`)
  and point permanent docs (this file, README, other feature docs) there – never to `PLAN.md`.
- Never commit `.env` or anything under `data/uploads/`, `data/policy_uploads/` or the `*.db` files.
