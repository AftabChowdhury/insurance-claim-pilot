"""Shared state passed between LangGraph nodes. Every node returns a partial update."""
import operator
import time
from typing import Annotated, Any, TypedDict


class Document(TypedDict, total=False):
    name: str
    path: str
    size: int
    extraction: str      # "text" | "pdf" | "pdf_ocr" | "docx" | "image" | "ocr_pending" | "unsupported" | "failed"
    text: str | None
    image: dict          # images only: fingerprints, exif, sanitised_path (api/evidence.py)


class ClaimState(TypedDict, total=False):
    # --- input (from the API) ---
    claim_id: str
    policy_number: str
    loss_type: str
    policy_type: str | None
    amount: float
    loss_date: str
    description: str
    documents: list[Document]

    customer_id: str | None

    # --- evidence (verify_evidence node) ---
    evidence: list[dict]         # per image / scanned PDF: vision reading, signals, verdict
    evidence_summary: dict       # counts, signals, documented total, has_usable_evidence
    evidence_injection: list[dict]  # text inside images that addresses an AI -> prompt injection

    # --- guardrails ---
    guard_in: dict
    blocked: bool
    clean_description: str
    clean_documents: list[dict]
    guard_out: dict

    # --- agents ---
    claim: dict              # intake agent output
    queries: list[str]
    candidates: list[dict]   # hybrid search results (all queries merged)
    ranked: list[dict]       # after semantic ranking + filtering
    context: str
    context_clauses: list[str]
    public_context: str          # context without confidential clauses (for the customer letter)
    confidential_clauses: list[str]
    coverage: dict
    fraud: dict
    letter: dict
    evaluation: dict

    # --- retry loop ---
    retries: int
    top_k: int
    min_reranker_score: float
    feedback: str

    # --- governance ---
    auto_decision: str           # approve | deny | need_info | review (guard_output)
    security_released: bool      # admin released a security-flagged claim for the normal check
    needs_review: bool
    review_reasons: list[str]
    human: dict
    final: dict

    # --- trace for the UI (append-only) ---
    events: Annotated[list[dict], operator.add]


def event(node: str, message: str, status: str = "done", **data: Any) -> dict:
    """Partial state update that appends one event for the UI timeline."""
    return {"events": [{"node": node, "status": status, "message": message, "ts": time.time(), "data": data}]}
