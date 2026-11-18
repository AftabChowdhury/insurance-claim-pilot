"""Shared state passed between LangGraph nodes. Every node returns a partial update."""
import operator
import time
from typing import Annotated, Any, TypedDict


class Document(TypedDict, total=False):
    name: str
    path: str
    size: int
    extraction: str      # "text" | "pdf" | "docx" | "ocr_pending" | "unsupported"
    text: str | None


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
    needs_review: bool
    review_reasons: list[str]
    human: dict
    final: dict

    # --- trace for the UI (append-only) ---
    events: Annotated[list[dict], operator.add]


def event(node: str, message: str, status: str = "done", **data: Any) -> dict:
    """Partial state update that appends one event for the UI timeline."""
    return {"events": [{"node": node, "status": status, "message": message, "ts": time.time(), "data": data}]}
