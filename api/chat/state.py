"""State of the policy-chat graph. One LangGraph thread per chat session (thread_id = "chat-<session_id>").

`history` persists across turns; every other field belongs to the current turn and is reset by turn_input().
"""
from typing import Annotated, Any, TypedDict

from api.graph.state import event  # noqa: F401 - same timeline event helper as the claims workflow


def add_or_reset(left: list | None, right: list | None) -> list:
    """Append reducer; an update of None clears the list (used to start each turn with an empty timeline)."""
    return [] if right is None else (left or []) + right


class ChatState(TypedDict, total=False):
    # --- input ---
    session_id: str
    message: str
    policy_filter: str | None        # UI selector (home | auto), None = all
    history: Annotated[list[dict], add_or_reset]  # [{role, content, status?}] across turns

    # --- input guardrail ---
    blocked: bool
    guard_in: dict
    clean_message: str               # PII-masked message: the only form of the message the LLMs see

    # --- query agent ---
    standalone_question: str
    queries: list[str]
    policy_type: list[str] | None    # e.g. ["home", "general"]

    # --- retrieval agent (public chunks only) ---
    candidates: list[dict]
    best_public_score: float
    ranked: list[dict]
    context: str
    context_clauses: list[str]
    sources: list[dict]              # public clauses in the context, for citations in the UI

    # --- confidential probe: scores + hashed shingles only, never text ---
    confidential_score: float
    confidential_shingles: list[str]

    # --- access control agent ---
    access: str                      # public | partial | restricted | not_found

    # --- answer + evaluation + retry ---
    answer: dict
    attempts: int
    evaluation: dict
    retries: int
    retry_target: str                # retrieval | generation
    feedback: str
    top_k: int | None
    min_reranker_score: float | None

    # --- output guardrail ---
    guard_out: dict
    guard_retries: int

    # --- final ---
    result: dict
    events: Annotated[list[dict], add_or_reset]


def turn_input(session_id: str, message: str, policy_filter: str | None) -> dict[str, Any]:
    """Graph input for a new turn: keeps `history` (checkpointer), resets every per-turn field."""
    return {
        "session_id": session_id, "message": message, "policy_filter": policy_filter,
        "events": None, "blocked": False, "guard_in": {}, "clean_message": "",
        "standalone_question": "", "queries": [], "policy_type": None,
        "candidates": [], "best_public_score": 0.0, "ranked": [], "context": "", "context_clauses": [], "sources": [],
        "confidential_score": 0.0, "confidential_shingles": [], "access": "",
        "answer": {}, "attempts": 0, "evaluation": {}, "retries": 0, "retry_target": "", "feedback": "",
        "top_k": None, "min_reranker_score": None, "guard_out": {}, "guard_retries": 0, "result": {},
    }
