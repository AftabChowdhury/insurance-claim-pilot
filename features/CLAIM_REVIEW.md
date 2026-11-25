# Feature: Claim check and claim review (multi-agent workflow)

A submitted claim runs through a **LangGraph workflow** (`api/graph/`) of guardrails, specialist agents and
deterministic checks. The workflow ends in one of two ways. Either code approves, denies or asks for more
information automatically, or the graph **pauses for an admin** (human in the loop), who accepts or rejects it in the
Admin panel. **The final status is always set by code (`guard_output`, `close_claim`) or by the admin, never by an
LLM.**

Related docs: [DOCUMENT_UPLOAD.md](DOCUMENT_UPLOAD.md) (upload validation, text extraction),
[SAVE_IN_VECTOR_DB.md](SAVE_IN_VECTOR_DB.md) (the policy index the agents search),
[POLICY_CHAT.md](POLICY_CHAT.md) (the separate chat graph, which shares `core/rag.py`).

## 1. Overview

```
Customer ── POST /customer/claims ──┐                     ┌─ automatic: APPROVED / DENIED / PENDING_INFO
Admin    ── POST /claims (demo) ────┤                     │
                                    ▼                     │
        save_uploads ─▶ create_claim ─▶ start_check ─▶ LangGraph run (thread_id = claim_id) ─┤
        (api/documents.py,              (api/claim_flow.py,  checkpointed in data/checkpoints.db)
         api/evidence.py)                status CHECKING)                     └─ interrupt(): AWAITING_REVIEW /
                                                                                FLAGGED_SECURITY
                                                                                     │
        Admin panel ▸ Claims review ── POST /claims/{id}/review ─▶ Command(resume=…) ┘ ─▶ close_claim
```

| Step | Code | What happens |
|---|---|---|
| Submit | `api/customer.py` `submit_claim`, `api/main.py` `POST /claims` | Form validation (own customer ID, loss type, date not in the future / > 3 years ago, rate limit `CUSTOMER_CLAIMS_PER_HOUR`), files saved under `data/uploads/<claim_id>/` |
| Upload processing | `api/documents.py`, `api/evidence.py` | Text extraction (PDF text layer, `.docx`, `.txt`, `.md`); images validated, fingerprinted (sha256 + dHash), EXIF / AI markers read, sanitised copy written to `_sanitised/` |
| Start | `api/claim_flow.py` `start_check` | Status `CHECKING`, builds the initial `ClaimState`, `Runner.start()` |
| Run | `api/runner.py` `Runner` | Runs `graph.astream()` as a background `asyncio` task; status = `running \| awaiting_review \| completed \| failed \| not_started` derived from the checkpoint |
| Restart safety | `claim_flow.resume_pending` (API startup) | Claims still `RECEIVED` / `CHECKING` are started, or continued from the last checkpoint; paused claims get their review status back |
| Review | `POST /claims/{id}/review`, `ui/claims_review.py` | Admin accepts / rejects / releases; resumes the paused graph |

## 2. Workflow graph

Wiring: `api/graph/graph.py` (`build_graph`). Mermaid export: `GET /graph?which=claims`.

```
START → verify_evidence → guard_input ─(blocked)→ human_review [security] ─(release)→ intake_agent
                              │                                  └(reject)→ close_claim
                              └→ intake_agent → rewrite_query → hybrid_search → semantic_rank → build_context
                                   → [coverage_agent ∥ fraud_agent] → writer_agent → evaluator_agent
                                        ─(fail, retries left)→ prepare_retry ↺ rewrite_query
                                   → guard_output ─(needs review)→ human_review [decision] → close_claim
                                                  └(automatic decision)──────────────────────→ close_claim → END
```

- **Parallelism:** `build_context` fans out to `coverage_agent` and `fraud_agent`; `writer_agent` waits for both
  (`g.add_edge(["coverage_agent", "fraud_agent"], "writer_agent")`).
- **State:** `ClaimState` (`api/graph/state.py`). Every node returns a partial update. `events` is append-only
  (`operator.add`); each node adds one entry with `event(node, message, status, **data)` for the UI timeline
  (`status` = `done | warn | fail`).
- **Persistence:** one LangGraph thread per claim (`thread_id = claim_id`), SQLite checkpointer. The run can pause
  at `interrupt()` for days and resume after an API restart.

## 3. Who does what

LLM calls go through `core.llm` (`structured(schema, mini=...)`, `chat()`), so the OpenAI / Azure switch keeps
working. "Mini" means the mini deployment.

| # | Node | Kind | Model | Input | Output (state) |
|---|---|---|---|---|---|
| 1 | `verify_evidence` | Checks + vision model | `chat()` vision | Images (sanitised copy), scanned PDFs | `evidence`, `evidence_summary`, `evidence_injection`, OCR text in `documents` |
| 2 | `guard_input` | Guardrail | regex + mini classifier | Description, file names, document / image text | `guard_in`, `blocked`, `clean_description`, `clean_documents` |
| 3 | `intake_agent` | **Agent** | mini → `ClaimFacts` | Form fields + masked description + documents | `claim` |
| 4 | `rewrite_query` | RAG step | mini → `SearchQueries` | `claim` (+ evaluator feedback on retry) | `queries` (3–5) |
| 5 | `hybrid_search` | RAG step | Azure AI Search | `queries`, `policy_type`, `access="all"` | `candidates` |
| 6 | `semantic_rank` | RAG step | (semantic ranker scores) | `candidates`, `top_k`, `min_reranker_score` | `ranked` |
| 7 | `build_context` | RAG step | — | `ranked` | `context`, `public_context`, `context_clauses`, `confidential_clauses` |
| 8 | `coverage_agent` | **Agent** | full → `CoverageDecision` | `claim`, evidence summary, `context` | `coverage` |
| 9 | `fraud_agent` | **Agent** + rules + MCP | mini → `FraudAssessment` | `claim`, history, watchlist, evidence signals | `fraud` |
| 10 | `writer_agent` | **Agent** | full → `DecisionLetter` | `claim`, `coverage`, `public_context` | `letter` |
| 11 | `evaluator_agent` | **Agent** (LLM-as-judge) + citation check | mini → `Judgement` | `claim`, `context`, `letter` | `evaluation` |
| 12 | `prepare_retry` | Self-correction | — | `evaluation` | `retries`, `feedback`, wider `top_k`, lower `min_reranker_score` |
| 13 | `guard_output` | Guardrail + decision rules | — (code only) | Everything above | `guard_out`, `auto_decision`, `needs_review`, `review_reasons`, `letter` |
| 14 | `human_review` | Human in the loop | — (`interrupt()`) | Review payload | `human`, `letter` (edited), `blocked`/`security_released` |
| 15 | `close_claim` | Persistence + MCP | — | `letter`, `human` | `final`; DB status, payout, letter, reason |

All agent prompts live in `api/graph/prompts.py`; the structured outputs in `api/graph/schemas.py`.

## 4. Node details

### 4.1 `verify_evidence` – evidence check (`api/graph/evidence.py`)

First node. Checks images (validated and fingerprinted at upload, `api/evidence.py`) and OCRs scanned PDFs.

1. **Reuse:** `db.find_similar_images` compares sha256 (identical file) and dHash Hamming distance
   (≤ `IMAGE_DUPLICATE_DISTANCE`) with images of other claims → `duplicate_same_customer` /
   `duplicate_other_customer`.
2. **Metadata:** EXIF date taken before the loss (`taken_before_loss`) or > 60 days after it; editor software
   (`edited_software`); AI generator software or C2PA / IPTC AI markers (`ai_generated`); no EXIF (`no_exif`).
3. **Vision model** (`IMAGE_EVIDENCE` prompt → `ImageEvidence`), only on the sanitised, metadata-free copy, at most
   `CLAIM_IMAGES_MAX` per claim, `VISION_CONCURRENCY` in parallel: image type, damage, whether it matches the
   declared loss, visible text, invoice data (date, total, currency, vendor), manipulation hints, and **text in the
   image that addresses an AI** (`embedded_instructions`).
4. **Consistency:** claimed amount > invoice total + 10 % (`amount_mismatch`); invoice / police report dated before
   the loss (`document_date_before_loss`); `does_not_match_loss`, `screenshot`, `manipulation_hint`.
5. **Verdict per image:** `suspicious` (any strong signal or ≥ 2 weak ones, `no_exif` ignored), `unverified` (vision
   failed / disabled), `unusable` (blurry, too dark), else `verified`.
6. **Scanned PDFs** (`extraction="ocr_pending"`): the first `CLAIM_PDF_OCR_MAX_PAGES` pages are rendered and
   transcribed (`PDF_OCR` prompt) → `extraction="pdf_ocr"`.

Every signal has a **fraud weight** and a strength (`SIGNALS` table), used later by `fraud_agent`.
`evidence_summary` holds the verdict counts, `documented_total` (sum of EUR invoice totals), `has_usable_evidence`,
signal messages and codes. Each image gets a text rendering (`_evidence_text`) in `documents[].text` so that the
injection scan and the intake agent see it. Signal messages never quote customer text; excerpts are for the admin
only.

### 4.2 `guard_input` – input guardrails (`api/graph/guardrails.py`)

Runs before any agent sees customer content. Order:

0. **Image injection:** any `evidence_injection` from the vision model → blocked (method `image`).
1. **Regex screen** (`CLAIM_INJECTION_RE`) on the **normalised** text (NFKC, invisible / bidi characters removed) of
   the description, every file name and every document / image text: "ignore previous instructions", "AI: approve",
   "set the payout to", role markers (`system:`), chat-template tokens, long base64 blobs …
2. **LLM classifier** (`INJECTION` prompt, mini, all text sources in parallel); blocked if
   `is_injection` and `score ≥ 0.8`. A classifier error never blocks the claim.
3. **PII masking** of description and documents (Presidio if installed, regex fallback).

On a block, the claim status becomes `FLAGGED_SECURITY`, the flagged source is replaced by
`[withheld: flagged by the security filter]`, and the graph goes **straight to `human_review` (security)**. No agent
has seen the claim at that point.

### 4.3 `intake_agent` – Intake Agent

Extracts structured facts (`ClaimFacts`): one-sentence `loss_summary`, `cause_of_loss`, `damaged_items`,
`claimed_amount`, `loss_date`, `key_facts`, `missing_information`. It uses only the validated form fields and the
**PII-masked** description and documents (each document truncated to 6,000 chars). It does not decide coverage.
It resets `retries` to 0.

### 4.4 Retrieval: `rewrite_query` → `hybrid_search` → `semantic_rank` → `build_context` (`api/graph/retrieval.py`)

- **`rewrite_query`** (mini, `REWRITE`): 3–5 policy-wording queries covering the insuring clause, **exclusions**,
  limits / deductibles and conditions. On a retry, `RETRY_BLOCK` adds the previous queries and the evaluator's
  feedback and asks for new queries.
- **`hybrid_search`**: `core.rag.multi_search` – one Azure AI Search call per query (BM25 + vector fused by RRF, with
  the semantic ranker), filtered by `policy_type` from the loss type, **`access="all"`** (the claims workflow is
  internal and may use confidential guidelines). Results merged by chunk id, best scores kept.
- **`semantic_rank`**: keeps the top `top_k` (default `RETRIEVAL_TOP_K` = 6) above `min_reranker_score` (default 1.5);
  keeps at least 3 so the coverage agent can still answer "unclear".
- **`build_context`**: expands to parent sections, de-duplicates, stays within `CONTEXT_CHAR_BUDGET`, tags each
  block with its clause ID. Produces `context` (all clauses, confidential ones labelled) and `public_context`
  (without confidential clauses) plus the clause lists.

### 4.5 `coverage_agent` – Coverage Analyst (runs in parallel with the fraud agent)

Full model, `COVERAGE` prompt → `CoverageDecision`. Decides from the policy context **only**:
`covered = yes | no | unclear`, `applicable_clauses`, `exclusions_applied`, `policy_limit`, `deductible`,
`payable_amount`, `reasoning`, `confidence`.

Rules in the prompt: check exclusions before concluding coverage (an exclusion overrides cover);
`payable_amount = min(claimed, policy limit, documented invoice total) − deductible`, never below 0; not enough
information → `unclear`. It also receives the system-generated evidence summary (`evidence_for_prompt`).

### 4.6 `fraud_agent` – Fraud Detector (runs in parallel with the coverage agent)

Hybrid of **deterministic rules**, **MCP tools** and a **mini LLM**:

1. **MCP tools** on `fraud_server` (port 8003) via `call_tool_or_fallback`: `check_prior_claims` (other claims on the
   policy; fallback `db.claim_history`) and `check_watchlist` (fallback: no signals).
2. **Rule score** (`_rule_fraud_score`), base 0.05:
   amount > 50,000 € (+0.3) or > 20,000 € (+0.1); ≥ 3 claims in the last 2 years (+0.25); watchlist hit (+0.25);
   fewer than 2 documents (+0.1); plus the weight of every **evidence signal type** from `verify_evidence`
   (e.g. photo reused by another customer +0.4, AI-generated +0.2). Capped at 0.95.
3. **LLM assessment** (`FRAUD` prompt → `FraudAssessment`) gets the rule score as a baseline, the history,
   watchlist and evidence signals.
4. **Final `risk_score = max(rule score, LLM score)`** – the LLM can raise the risk but never lower it below the
   rules. Level: `high` ≥ `FRAUD_REVIEW_THRESHOLD` (0.7), `medium` ≥ 0.3, else `low`.

The fraud result is **internal**: it never goes to the letter writer.

### 4.7 `writer_agent` – Letter Writer

Full model, `WRITER` prompt → `DecisionLetter`: `decision` (`approve | deny | need_info`), `payout`, a 1–2 sentence
customer-facing `reason`, the `letter` with inline `[Clause N.M]` citations, and `citations`.

- Gets the `claim`, the `coverage` analysis and **only `public_context`**, so confidential clauses cannot be quoted.
- `approve` only if covered = yes, `deny` only if covered = no, otherwise `need_info`; payout = the coverage
  analyst's `payable_amount`.
- Denials must mention the 60-day appeal; no fraud scores, image forensics, legal advice or promises; < 200 words.

### 4.8 `evaluator_agent` – Evaluator (LLM-as-judge) and the retry loop

Mini model, `JUDGE` prompt → `Judgement`: `faithfulness`, `answer_relevance`, `citation_coverage`,
`unsupported_statements`, `missing_considerations` (e.g. an unchecked exclusion), `feedback`.

A **deterministic citation check** runs on top: a cited clause that is not in `context_clauses` caps
`citation_coverage` at 0.5. The letter **passes** if `faithfulness ≥ EVAL_THRESHOLD` (0.8), `citation_coverage ≥ 0.7`
and `answer_relevance ≥ 0.6`.

`route_after_eval`: pass → `guard_output`; fail with retries left (`MAX_RETRIES` = 2) → **`prepare_retry`**, which
feeds the judge's feedback and missing considerations into the next query rewrite, widens retrieval
(`top_k + 3`) and lowers `min_reranker_score` (−0.5, minimum 0.5), then loops back to `rewrite_query`. Out of
retries → `guard_output`, which escalates to the admin.

### 4.9 `guard_output` – output guardrails and decision rules

Pure code, no LLM. It first collects **violations** in the letter:

- approve / deny without clause citations; citations not in the retrieved context; citations of **confidential**
  clauses;
- **decision integrity:** approve although coverage ≠ yes, deny although coverage = yes, payout without approval,
  payout > claimed amount / policy limit / documented invoice total, approved payout ≠ coverage `payable_amount`
  (± 1 €);
- denial without appeal rights, prohibited wording (guarantees, legal advice), **internal information** in the letter
  or reason (fraud, risk score, EXIF, metadata, confidential, "as an AI" …).

It then collects **escalation reasons**:

| Reason | Rule |
|---|---|
| Released security claim | `security_released` → the final decision is always the admin's |
| High amount | claimed amount or payout > `HITL_PAYOUT_THRESHOLD` (5,000 €) |
| Fraud | `risk_score ≥ FRAUD_REVIEW_THRESHOLD` (0.7) |
| Suspicious evidence | any image with verdict `suspicious` |
| Unverified evidence | images the vision model could not analyse |
| Evaluation failed | still failing after all retries |
| Coverage unclear | `covered = "unclear"` |
| Violations | every violation above |

Evaluation, coverage and violation reasons only count when there is usable evidence.

**Decision rules** (`auto_decision`):

1. any reason → `review` (status `AWAITING_REVIEW`, reasons in the audit note) → `human_review`;
2. no usable evidence (nothing readable) → `need_info` with a standard "please upload readable photos and the
   invoice" letter;
3. otherwise the checked letter decision is applied automatically: `approve` / `deny` / `need_info` → `close_claim`.

So the automatic path only approves claims ≤ 5,000 € with a passing evaluation, clear coverage, low fraud risk,
clean evidence and a letter without violations.

### 4.10 `human_review` – admin review (`api/graph/human.py`)

Calls `interrupt()` with a review payload: `kind` (`security` or `decision`), `reasons`, the agents' proposed
`decision` / `payout` / `reason` / `draft_letter`, `fraud`, `evidence` and `evaluation` (and the `guard` details for
security claims). The run stops; `Runner.status()` reports `awaiting_review`.

The admin resumes it with `POST /claims/{id}/review` → `Command(resume={action, reason, payout, letter, note,
reviewer})`. The API checks that the claim is awaiting review, the payout does not exceed the claimed amount, and
`release` is only used for security claims. Every action is written to the audit log (`review:<action>`).

| Kind | Action | Effect |
|---|---|---|
| security | **Release** (or Accept, treated as release) | `blocked=False`, `security_released=True` → back to `intake_agent`; the flagged content stays withheld; the claim later returns for a final admin decision |
| security | **Reject** | Denial letter with the admin's reason → `close_claim` |
| decision | **Accept** | Decision `approve` with the admin's payout. The agents' draft letter is kept (or the admin's edit used) if it was an approval with the same amount; otherwise a standard acceptance letter is generated |
| decision | **Reject** | Decision `deny`, payout 0, the admin's reason (required in the UI) and a rejection letter with appeal rights |

Because a resumed node runs again from the top, nothing with side effects happens before `interrupt()`.

### 4.11 `close_claim` – close-out

Maps the decision to the claim status (`approve → APPROVED`, `deny → DENIED`, `need_info → PENDING_INFO`, unknown →
`ESCALATED`; admin reject → `DENIED`). Payout is only kept for `APPROVED`. Writes status, payout, `decided_by`
(`claimpilot` for automatic decisions, otherwise the admin) and reason via the **claims MCP server** (port 8001,
`update_claim_status`, `save_decision_letter`) with local DB fallback, and saves the customer-facing
`decision_reason`. `final.automatic` tells the UI whether a human decided.

## 5. Trust and safety measures

| Risk | Measure |
|---|---|
| Prompt injection in description, documents, file names or image text | `guard_input` (normalisation, regex, LLM classifier, vision `embedded_instructions`); blocked claims never reach an agent |
| Injection that gets past the screen | **Spotlighting:** every prompt wraps customer content in `<untrusted_customer_input>` and includes `UNTRUSTED_RULE` |
| PII sent to LLMs | Masked before the intake agent; vision model only gets the sanitised copy without EXIF / GPS |
| Hallucinated policy statements | Answers grounded in retrieved clauses, evaluator + citation check, retry loop, `guard_output` citation checks |
| LLM approves too much | Decision and payout cross-checked against coverage, claimed amount, policy limit and invoice total in code; HITL above 5,000 € |
| Fraud score lowered by the LLM | `risk_score = max(rules, LLM)` |
| Confidential guideline leaks | Writer gets `public_context` only; `guard_output` escalates letters citing confidential clauses |
| Internal checks revealed to the customer | Writer prompt + `INTERNAL` regex in `guard_output`; customers see a reduced status (§6) |
| MCP server down | `call_tool_or_fallback` uses the local DB |

## 6. Statuses

| Internal status (DB) | Set by | Customer sees (`api/customer.py`) |
|---|---|---|
| `RECEIVED` | `create_claim` | Checking |
| `CHECKING` | `start_check` | Checking |
| `FLAGGED_SECURITY` | `guard_input` block | In review |
| `AWAITING_REVIEW` | `guard_output` escalation | In review |
| `ESCALATED` | `close_claim` (unknown decision) | In review |
| `APPROVED` / `DENIED` / `PENDING_INFO` | `close_claim` | Decision, payout (approved only), reason and letter |

## 7. Admin panel: Claims review (`ui/claims_review.py`)

- Claim list with claims waiting for a decision (`AWAITING_REVIEW`, `FLAGGED_SECURITY`) first.
- Header metrics: claimed amount, decision, payout, evaluation (faithfulness), fraud risk, suspicious evidence.
- **Agent timeline** from `events`, refreshed every 2 s while the run is going (`GET /claims/{id}`).
- Tabs: Letter, Evidence (per image: verdict, signals, vision reading, EXIF, original file), Coverage (+ fraud
  assessment), Retrieved clauses (with reranker scores), Evaluation, Guardrails (input / output), Record.
- **Review panel** when the run is paused: the escalation reasons and the agents' proposal; security claims show the
  flagged source and excerpt. Buttons: Accept (editable payout and letter), Reject (reason for the customer
  required), Release (security claims). An internal note goes to the audit log only.

## 8. API

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /customer/claims` | customer | Submit a claim; starts the check |
| `GET /customer/claims`, `GET /customer/claims/{id}` | customer | Own claims with the reduced status |
| `POST /claims` | admin | Submit a claim (demo / testing) |
| `GET /claims`, `GET /claims/{id}` | admin | List; full state (events, evidence, coverage, fraud, letter, evaluation, guardrails, review request) |
| `GET /claims/{id}/events` | admin | SSE stream of timeline events until the run stops or pauses |
| `POST /claims/{id}/review` | admin | Accept / reject / release; resumes the graph |
| `GET /claims/{id}/files/{name}` | admin | Original uploaded file |
| `GET /claims/{id}/letter`, `GET /claims/{id}/audit` | admin | Decision letter, audit trail |
| `GET /graph?which=claims` | open | Mermaid diagram of the graph |

## 9. Configuration (`core/config.py`, `.env`)

| Setting | Default | Used by |
|---|---|---|
| `HITL_PAYOUT_THRESHOLD` | 5000 | `guard_output` (claimed amount / payout above → admin) |
| `FRAUD_REVIEW_THRESHOLD` | 0.7 | `fraud_agent` level, `guard_output` |
| `EVAL_THRESHOLD` | 0.8 | `evaluator_agent` (faithfulness) |
| `MAX_RETRIES` | 2 | Retry loop |
| `RETRIEVAL_TOP_K`, `MIN_RERANKER_SCORE`, `CONTEXT_CHAR_BUDGET` | 6, 1.5, 24000 | Retrieval |
| `CLAIM_VISION`, `CLAIM_IMAGES_MAX`, `CLAIM_PDF_OCR_MAX_PAGES`, `VISION_CONCURRENCY` | true, 10, 5, 4 | `verify_evidence` |
| `CLAIM_IMAGE_MIN_PX`, `CLAIM_IMAGE_MAX_SIDE`, `CLAIM_IMAGE_MAX_MPX`, `IMAGE_DUPLICATE_DISTANCE` | 200, 2048, 40, 6 | Upload check / reuse detection |
| `CUSTOMER_CLAIMS_PER_HOUR` | 5 | `POST /customer/claims` rate limit |
| `CLAIMS_MCP_URL`, `FRAUD_MCP_URL` | `localhost:8001/8003` | `close_claim`, `fraud_agent` |

## 10. Verifying

There is no unit test suite. Run `.\run_all.ps1`, then submit the demo scenarios from `data/sample_claims/README.md`
(Submit Claim page or `POST /claims`) and follow them in Admin ▸ Claims review: an automatic approval ≤ 5,000 €,
an exclusion denial, a high-amount claim that waits for the admin, an injection attempt that is flagged for the
security review, and a claim with a raised fraud score (prior claims on the policy). Traces are in LangSmith when `LANGSMITH_*` is set.
