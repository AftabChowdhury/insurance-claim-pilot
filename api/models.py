"""Request/response models for the HTTP API."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ClaimCreated(BaseModel):
    claim_id: str
    thread_id: str
    status: str
    documents: list[dict]


class ReviewDecision(BaseModel):
    """Admin decision on a paused claim. accept = approve (payout / letter editable); reject = deny with a reason the
    customer sees; release = security-flagged claim goes through the normal check (flagged content withheld).
    approve / edit are accepted as aliases of accept."""
    action: Literal["accept", "reject", "release", "approve", "edit"]
    reason: str = Field("", max_length=1000, description="Customer-facing reason (required for reject)")
    letter: str | None = Field(None, max_length=10_000, description="Edited decision letter (optional)")
    payout: float | None = Field(None, ge=0)
    note: str = Field("", max_length=1000, description="Internal note for the audit log")

    @model_validator(mode="after")
    def _reason_for_reject(self):
        if self.action == "reject" and len(self.reason.strip()) < 5:
            raise ValueError("A reason (at least 5 characters) is required to reject a claim")
        return self


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
    evidence: list[dict] = []
    evidence_summary: dict | None = None
    auto_decision: str | None = None
