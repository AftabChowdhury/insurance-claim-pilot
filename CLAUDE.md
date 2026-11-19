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
| Claim documents: images and scanned PDFs | **TODO** – marked `ocr_pending`; needs OCR / vision model (Azure Document Intelligence or a multimodal LLM) |
| Policy chat | **TODO** – no chat endpoint or UI yet; reuse `core/search.py` + `api/graph/retrieval.py` patterns and cite `clause_id`s |
| Admin panel for policy upload | Implemented – Streamlit Admin page + `/admin/policies` (upload `.pdf`/`.doc`/`.docx`, list, delete) storing files in `data/policy_uploads/` |
| Extraction of policy documents (`.pdf`/`.docx`/`.doc`/`.md`, images, workflow diagrams, scanned pages) | Implemented – `ingest/extract/` + `ingest/figures.py` (vision model = chat deployment); CLI `uv run python -m ingest.extract`; see `PLAN.md` |
| Chunking of policy documents | Implemented – `ingest/structure.py` + `ingest/chunking.py` (clause-based, token limits); CLI `uv run python -m ingest.chunking` |
| Embedding / indexing of policies | Implemented – `ingest/indexer.py` + `ingest/pipeline.py`; built-in policies via `ingest/ingest_policies.py`; admin delete removes chunks |
| Automatic indexing after admin upload + resync | Implemented – background worker `api/indexing.py` (queue, resync on API start), Admin page status / re-index / resync / chunk preview (`PLAN.md` §7) |
| Authentication / admin vs. user roles | Partial – static admin login (`api/auth.py`, HMAC bearer token); claim endpoints are open |

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
  documents.py      Upload validation (types, 10 MB, max 10 files) + text extraction
  auth.py           Static admin login (POST /admin/login) + require_admin bearer-token dependency
  admin.py          /admin/policies: upload (.pdf/.doc/.docx), list, delete, resync, re-index, chunk preview
  indexing.py       IndexWorker: background queue that indexes uploads; resync on startup
  files.py          safe_name() for uploaded file names
  runner.py         Background graph runs, status (running / awaiting_review / completed / failed)
  mcp_client.py     MCP tool calls with fallback
  graph/            LangGraph workflow
    graph.py        Graph wiring (see diagram in module docstring)
    state.py        ClaimState TypedDict + event() helper for the UI timeline
    agents.py       intake, coverage, fraud, writer, evaluator agents + retry routing
    retrieval.py    rewrite_query → hybrid_search → semantic_rank → build_context
    guardrails.py   input (injection, PII) and output policy checks
    human.py        interrupt() for adjuster review, close_claim
    prompts.py, schemas.py
core/               Shared: config (pydantic-settings), llm clients, search, db, domain (loss types)
ingest/             ingest_policies.py – index data/policies/ with the shared pipeline (doc_id builtin-<stem>)
  pipeline.py       extract → describe → chunk → embed → index; process_policy_doc(doc_id) for uploads
  indexer.py        index schema, batched embedding with retry, upsert / delete chunks by doc_id
  extract/          policy file → ExtractedDocument (ordered blocks: heading/paragraph/list/table/figure);
                    pdf.py (pymupdf4llm layout), word.py (.docx walk, .doc via LibreOffice), markdown.py
  figures.py        vision model: figure descriptions (workflow steps), scanned-page OCR, figures.json cache
  structure.py      blocks → sections/clauses (clause_id, parent_content, policy_type)
  chunking.py       clause-based chunks with token limits, figure chunks → index-shaped dicts (+ CLI)
mcp_servers/        claims_server (8001), policy_server (8002), fraud_server (8003)
ui/app.py           Streamlit entrypoint (st.navigation): claims_page.py (submit claim, timeline,
                    adjuster review) and admin_page.py (login, policy upload/list/delete); common.py api()
eval/               retrieval_eval.py + testset.json (keyword vs vector vs hybrid vs hybrid+semantic)
data/               policies/, sample_claims/, fraud_watchlist.json, uploads/, policy_uploads/ and SQLite DBs (runtime)
```

## Claim workflow (LangGraph)

```
guard_input ─(blocked)→ close_claim
  → intake_agent → rewrite_query → hybrid_search → semantic_rank → build_context
  → [coverage_agent ∥ fraud_agent] → writer_agent → evaluator_agent
      ─(score < EVAL_THRESHOLD, retries left)→ prepare_retry → rewrite_query
  → guard_output ─(payout > HITL_PAYOUT_THRESHOLD or fraud ≥ FRAUD_REVIEW_THRESHOLD)→ human_review
  → close_claim → END
```

Nodes return partial state updates; append UI timeline entries with `event(node, message, status)`.

## Policy document conventions

Policies are chunked by markdown headings (after PDF → markdown conversion):

- `## Section N – Title` → section (parent context, stored in `parent_content`)
- `### N.M Clause title` → clause chunk, `clause_id = "N.M"`
- `policy_type` from front matter (`policy_type: home|auto`) or filename prefix (`home_`, `auto_`), else `general`.

Index fields: `id, clause_id, title, section, content, parent_content, source, policy_type, content_vector`.
Answers and letters must cite `clause_id`s. New ingestion paths (admin upload, `.docx`, images) should produce
the same chunk shape so retrieval keeps working.

## Commands

```powershell
uv sync                                              # install deps
copy .env.example .env                               # fill OPENAI_API_KEY, AZURE_SEARCH_KEY
uv run python -m ingest.ingest_policies --recreate   # (re)build search index from data/policies/
uv run python -m ingest.ingest_policies --dry-run    # print chunks only
uv run python -m ingest.pipeline --pending           # resync uploads without the API (--doc-id <id> / --all to force)
.\run_all.ps1                                        # start 3 MCP servers + API + UI in separate windows
.\stop_all.ps1                                       # stop them all and close their windows
uv run uvicorn api.main:app --reload --port 8000     # API only (docs: /docs, graph: /graph)
uv run streamlit run ui/app.py                       # UI only (http://localhost:8501)
uv run python -m eval.retrieval_eval                 # retrieval comparison
docker compose up --build                            # everything in containers
```

There is no unit test suite yet; verify changes by running the API/UI and the demo scenarios in
`data/sample_claims/README.md`.

## Conventions

- All settings go through `core.config.get_settings()`; add new ones there and to `.env.example`.
- Use `core.llm.chat(mini=...)` / `structured(schema)` – never instantiate model clients directly, so the
  OpenAI/Azure switch keeps working. Mini model for guardrails, query rewrite and judging.
- Treat uploaded documents as untrusted: they pass through `guard_input` (injection + PII) before reaching agents.
- Keep MCP tools usable with fallback (`call_tool_or_fallback`) so the demo runs without the MCP servers.
- Never commit `.env` or anything under `data/uploads/`, `data/policy_uploads/` or the `*.db` files.
