"""Customer API: profile, submit a claim request, own claims + status (login: api/auth.py POST /customer/login).

A customer claim is only stored (status RECEIVED, files in settings.upload_dir/<claim_id>/). The claim check
(guardrails + multi-agent workflow) is not started here; that comes in a later feature.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core import db
from core.domain import LOSS_TYPES, label_for

from .auth import require_customer
from .documents import save_uploads

router = APIRouter(prefix="/customer", tags=["customer"])

# Internal claim status -> (status shown to the customer, label). Internal states such as a security flag or an
# escalation are shown as "being reviewed" so the customer never learns about fraud / injection checks.
CUSTOMER_STATUS = {
    "RECEIVED": ("RECEIVED", "Received – waiting for review"),
    "APPROVED": ("APPROVED", "Approved"),
    "DENIED": ("DENIED", "Declined"),
    "PENDING_INFO": ("PENDING_INFO", "More information needed"),
    "ESCALATED": ("IN_REVIEW", "Being reviewed by a claims adjuster"),
    "FLAGGED_SECURITY": ("IN_REVIEW", "Being reviewed by a claims adjuster"),
}
FINAL = {"APPROVED", "DENIED", "PENDING_INFO"}  # decision (payout, letter) is visible to the customer


def _doc_meta(docs: list[dict]) -> list[dict]:
    return [{k: d[k] for k in ("name", "size", "extraction") if k in d} for d in docs]


@router.get("/me")
def me(customer: dict = Depends(require_customer)):
    return {k: customer[k] for k in ("id", "name", "email", "phone", "address", "policy_number")}


@router.post("/claims", status_code=201)
async def submit_claim(
    customer_id: str = Form(...),
    loss_type: str = Form(...),
    amount: float = Form(..., gt=0, le=10_000_000),
    description: str = Form(..., min_length=10, max_length=5000),
    files: list[UploadFile] = File(...),
    customer: dict = Depends(require_customer),
):
    """Submit a claim request with supporting documents. Stored as RECEIVED; no claim check is run yet."""
    if customer_id.strip() != customer["id"]:
        raise HTTPException(403, "You can only submit claims for your own customer ID")
    if loss_type not in LOSS_TYPES:
        raise HTTPException(422, f"loss_type must be one of {list(LOSS_TYPES)}")
    if not customer.get("policy_number"):
        raise HTTPException(422, "No policy is linked to your customer account")

    claim_id = db.new_claim_id()
    docs = await save_uploads(claim_id, files)
    record = db.create_claim(
        {"id": claim_id, "policy_number": customer["policy_number"], "loss_type": loss_type, "amount": amount,
         "description": description.strip(), "documents": _doc_meta(docs), "customer_id": customer["id"]},
        actor=f"customer:{customer['id']}")
    return {"claim_id": claim_id, "status": record["status"], "documents": _doc_meta(docs)}


def _summary(c: dict) -> dict:
    status, label = CUSTOMER_STATUS.get(c["status"], ("IN_REVIEW", "Being reviewed"))
    return {"id": c["id"], "policy_number": c["policy_number"], "loss_type": c["loss_type"],
            "loss_type_label": label_for(c["loss_type"]), "amount": c["amount"], "status": status,
            "status_label": label, "payout": c["payout"] if status in FINAL else None,
            "created_at": c["created_at"], "updated_at": c["updated_at"],
            "documents": [d.get("name") for d in c["documents"]]}


@router.get("/claims")
def my_claims(customer: dict = Depends(require_customer)):
    """The logged-in customer's own claims, newest first."""
    return [_summary(c) for c in db.list_claims(customer_id=customer["id"], limit=200)]


@router.get("/claims/{claim_id}")
def my_claim(claim_id: str, customer: dict = Depends(require_customer)):
    """One of the customer's own claims with description and, once decided, the decision letter."""
    c = db.get_claim(claim_id)
    if not c or c.get("customer_id") != customer["id"]:
        raise HTTPException(404, "Claim not found")  # same answer for other customers' claims
    out = _summary(c)
    out["loss_date"] = c["loss_date"]
    out["description"] = c["description"]
    out["letter"] = c["letter"] if out["status"] in FINAL else None
    return out
