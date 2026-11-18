"""Request/response models for the HTTP API."""
from typing import Literal

from pydantic import BaseModel, Field


class ClaimCreated(BaseModel):
    claim_id: str
    thread_id: str
    status: str
    documents: list[dict]


class ReviewDecision(BaseModel):
    action: Literal["approve", "edit", "reject"]
    letter: str | None = Field(None, description="Edited letter text (for approve/edit)")
    decision: Literal["approve", "deny", "need_info"] | None = None
    payout: float | None = None
    reviewer: str = "adjuster"
    note: str = ""


class ClaimView(BaseModel):
    claim_id: str
    status: str                 # running | awaiting_review | completed | failed | not_started
    error: str | None = None
    record: dict | None = None  # row in the claims system
    events: list[dict] = []
    review_request: dict | None = None
    guard_in: dict | None = None
    claim: dict | None = None
    ranked: list[dict] = []
    coverage: dict | None = None
    fraud: dict | None = None
    letter: dict | None = None
    evaluation: dict | None = None
    guard_out: dict | None = None
    human: dict | None = None
    final: dict | None = None
    retries: int = 0
