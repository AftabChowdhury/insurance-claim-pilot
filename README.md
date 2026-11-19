# ClaimPilot

Multi-agent insurance claims copilot. A customer submits a claim with documents; agents retrieve the relevant
policy clauses, decide coverage, check fraud, write a cited decision letter, evaluate it (and retry if weak),
and pause for a human adjuster when the stakes are high.

| Requirement | Where |
|---|---|
| Advanced RAG | `api/graph/retrieval.py` – multi-query rewrite, parent-section expansion, token budget, clause-tagged context |
| Hybrid search | `core/search.py` – BM25 + vector in one Azure AI Search query, fused with RRF |
| Semantic ranking | Azure AI Search semantic ranker (`query_type="semantic"`), filtered by `MIN_RERANKER_SCORE` |
| Retrieve → Rank → Context → Generate | `rewrite_query → hybrid_search → semantic_rank → build_context → writer_agent` |
| Evaluate | `evaluator_agent` (LLM judge: faithfulness, relevance, citation coverage) + `eval/retrieval_eval.py` |
| Retry | `route_after_eval → prepare_retry → rewrite_query` with judge feedback and wider retrieval |
| Agents / orchestration | `api/graph/graph.py` – LangGraph with parallel coverage + fraud agents |
| Guardrails | `api/graph/guardrails.py` – injection (regex + LLM), PII (Presidio/regex), output policy checks |
| Human in the loop | `api/graph/human.py` – `interrupt()` + SQLite checkpointer, resumed by `POST /claims/{id}/review` |
| MCP | `mcp_servers/` – claims, policy search, fraud servers; agents call them via `api/mcp_client.py` |
| Observability | LangSmith (set `LANGSMITH_*` in `.env`) |

## Setup

```powershell
cd claimpilot
uv sync
copy .env.example .env   # then fill in OPENAI_API_KEY and AZURE_SEARCH_KEY
```

Load the sample policies into Azure AI Search (creates the index with vector + semantic configuration):

```powershell
uv run python -m ingest.ingest_policies --recreate
```

## Run

All services in separate windows:

```powershell
.\run_all.ps1
.\stop_all.ps1   # stop all services and close their windows
```

Or one by one: `uv run python -m mcp_servers.claims_server` (8001), `...policy_server` (8002), `...fraud_server` (8003),
`uv run uvicorn api.main:app --port 8000`, `uv run streamlit run ui/app.py` (8501).

Or with Docker: `docker compose up --build`.

- UI: http://localhost:8501
- API docs: http://localhost:8000/docs
- Agent graph: http://localhost:8000/graph (Mermaid)

The MCP servers are optional for a run: if one is down, the agents fall back to local calls and say so in the timeline.

## Admin Panel (policy documents)

Open the **Admin** page in the UI (http://localhost:8501/admin) and log in with `Admin` / `admin`
(change via `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_TOKEN_SECRET` in `.env`).

- Upload one or more policy documents at once: `.pdf`, `.doc`, `.docx` only, up to 25 MB each and 20 per upload.
  Files whose content does not match the extension, and exact duplicates, are rejected.
- Files are stored in `data/policy_uploads/` as `<doc_id>_<filename>` and registered in the `policy_documents`
  table of `data/claimpilot.db` with status `uploaded`.
- The list shows every uploaded document; delete one (🗑) or several ("Delete selected").

Uploaded documents are **not indexed yet** – parsing and indexing into Azure AI Search come in a later step.
API: `POST /admin/login`, then `GET|POST /admin/policies`, `DELETE /admin/policies/{doc_id}` with
`Authorization: Bearer <token>`.

## Demo

Sample documents and form values for four scenarios are in [data/sample_claims/README.md](data/sample_claims/README.md):
clean auto-approval, evaluator retry (flood exclusion), blocked prompt injection, and adjuster review ($82k theft).

Retrieval comparison for the slides:

```powershell
uv run python -m eval.retrieval_eval
```

## Using the MCP servers from Claude Desktop

Add to `claude_desktop_config.json` (adjust the path):

```json
{
  "mcpServers": {
    "claimpilot-claims": {
      "command": "uv",
      "args": ["run", "--directory", "C:/NTT-Projects/Hackathon-11-2026/claimpilot", "python", "-m", "mcp_servers.claims_server", "--stdio"]
    }
  }
}
```

## Optional: Presidio PII masking

```powershell
uv sync --extra pii
uv run python -m spacy download en_core_web_sm
```

Without it, a regex fallback masks emails, phone numbers, card numbers and IBANs.

## Known TODOs

- OCR for images and scanned PDFs (Azure Document Intelligence) – currently marked `ocr_pending`
- RAGAS end-to-end evaluation on a labelled set of claims
- Authentication for the API and the adjuster role
- Azure deployment: Container Apps, Azure OpenAI, Azure PostgreSQL (checkpointer + claims DB), Key Vault
