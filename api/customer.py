"""Customer API: profile, submit a claim (starts the claim check), own claims + status (login: api/auth.py).

A submitted claim is stored (files in settings.upload_dir/<claim_id>/) and the claim check (evidence check,
guardrails, multi-agent workflow) starts at once. Customers see a reduced status: internal states such as a
security flag, a fraud review or the admin queue are all shown as "in review".
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from core import db
from core.config import get_settings
from core.domain import LOSS_TYPES, label_for

from .auth import require_customer
from .claim_flow import start_check
from .documents import save_uploads

router = APIRouter(prefix="/customer", tags=["customer"])

# Internal claim status -> (status shown to the customer, label).
IN_REVIEW = ("IN_REVIEW", "Being reviewed by a claims adjuster")
CUSTOMER_STATUS = {
    "RECEIVED": ("CHECKING", "Received – being checked"),
    "CHECKING": ("CHECKING", "Being checked"),
    "APPROVED": ("APPROVED", "Approved"),
    "DENIED": ("DENIED", "Declined"),
    "PENDING_INFO": ("PENDING_INFO", "More information needed"),
    "AWAITING_REVIEW": IN_REVIEW,
    "ESCALATED": IN_REVIEW,
    "FLAGGED_SECURITY": IN_REVIEW,
}
FINAL = {"APPROVED", "DENIED", "PENDING_INFO"}  # decision (payout, reason, letter) is visible to the customer


def _doc_meta(docs: list[dict]) -> list[dict]:
    return [{k: d[k] for k in ("name", "size", "extraction") if k in d} for d in docs]


@router.get("/me")
def me(customer: dict = Depends(require_customer)):
    return {k: customer[k] for k in ("id", "name", "email", "phone", "address", "policy_number")}


@router.post("/claims", status_code=201)
async def submit_claim(
    request: Request,
    customer_id: str = Form(...),
    loss_type: str = Form(...),
    amount: float = Form(..., gt=0, le=10_000_000),
    loss_date: date = Form(...),
    description: str = Form(..., min_length=10, max_length=5000),
    files: list[UploadFile] = File(...),
    customer: dict = Depends(require_customer),
):
    """Submit a claim with supporting documents / photos. The claim check starts immediately (status CHECKING)."""
    if customer_id.strip() != customer["id"]:
        raise HTTPException(403, "You can only submit claims for your own customer ID")
    if loss_type not in LOSS_TYPES:
        raise HTTPException(422, f"loss_type must be one of {list(LOSS_TYPES)}")
    if loss_date > date.today():
        raise HTTPException(422, "The date of loss cannot be in the future")
    if loss_date < date.today() - timedelta(days=3 * 365):
        raise HTTPException(422, "The date of loss is more than 3 years ago")
    if not customer.get("policy_number"):
        raise HTTPException(422, "No policy is linked to your customer account")
    limit = get_settings().customer_claims_per_hour
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if db.count_claims_since(customer["id"], since) >= limit:
        raise HTTPException(429, f"You can submit at most {limit} claims per hour. Please try again later.")

    claim_id = db.new_claim_id()
    docs = await save_uploads(claim_id, files)
    record = db.create_claim(
        {"id": claim_id, "policy_number": customer["policy_number"], "loss_type": loss_type, "amount": amount,
         "loss_date": loss_date.isoformat(), "description": description.strip(), "documents": _doc_meta(docs),
         "customer_id": customer["id"]},
        actor=f"customer:{customer['id']}")
    start_check(request.app.state.runner, record, docs)
    return {"claim_id": claim_id, "status": "CHECKING", "documents": _doc_meta(docs)}


def _summary(c: dict) -> dict:
    status, label = CUSTOMER_STATUS.get(c["status"], IN_REVIEW)
    return {"id": c["id"], "policy_number": c["policy_number"], "loss_type": c["loss_type"],
            "loss_type_label": label_for(c["loss_type"]), "amount": c["amount"], "loss_date": c["loss_date"],
            "status": status, "status_label": label, "payout": c["payout"] if status == "APPROVED" else None,
            "reason": c.get("decision_reason") if status in FINAL else None,
            "created_at": c["created_at"], "updated_at": c["updated_at"],
            "documents": [d.get("name") for d in c["documents"]]}


@router.get("/claims")
def my_claims(customer: dict = Depends(require_customer)):
    """The logged-in customer's claim history, newest first."""
    return [_summary(c) for c in db.list_claims(customer_id=customer["id"], limit=200)]


@router.get("/claims/{claim_id}")
def my_claim(claim_id: str, customer: dict = Depends(require_customer)):
    """One of the customer's own claims with description and, once decided, reason + decision letter."""
    c = db.get_claim(claim_id)
    if not c or c.get("customer_id") != customer["id"]:
        raise HTTPException(404, "Claim not found")  # same answer for other customers' claims
    out = _summary(c)
    out["description"] = c["description"]
    out["letter"] = c["letter"] if out["status"] in FINAL else None
    return out
