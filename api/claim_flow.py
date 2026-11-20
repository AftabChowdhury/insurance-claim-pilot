"""Starting the claim check (LangGraph workflow) for a stored claim, shared by POST /claims (admin demo) and
POST /customer/claims, plus the startup resync that restarts unfinished customer claims after an API restart."""
import logging

from core import db
from core.domain import STATUS_AWAITING_REVIEW, STATUS_CHECKING, STATUS_FLAGGED, policy_type_for

from .documents import load_documents
from .runner import Runner

log = logging.getLogger(__name__)


def initial_state(record: dict, docs: list[dict]) -> dict:
    return {
        "claim_id": record["id"], "policy_number": record["policy_number"], "loss_type": record["loss_type"],
        "policy_type": policy_type_for(record["loss_type"]), "amount": float(record["amount"]),
        "loss_date": record.get("loss_date") or "", "description": record.get("description") or "",
        "documents": docs, "customer_id": record.get("customer_id"), "retries": 0, "events": [],
    }


def start_check(runner: Runner, record: dict, docs: list[dict], actor: str = "claimpilot") -> None:
    db.set_status(record["id"], STATUS_CHECKING, actor, "claim check started")
    runner.start(record["id"], initial_state(record, docs))


async def resume_pending(runner: Runner) -> None:
    """Customer claims still RECEIVED / CHECKING: start the check, or continue it from the last checkpoint."""
    for record in db.claims_to_resume():
        cid = record["id"]
        try:
            status, _, review = await runner.status(cid)
            if runner.is_running(cid):
                continue
            if status == "not_started":
                docs = load_documents(cid, [d["name"] for d in record["documents"]])
                start_check(runner, record, docs, "resync")
                log.info("Resync: started claim check for %s", cid)
            elif status == "awaiting_review":
                kind = (review or {}).get("kind")
                db.set_status(cid, STATUS_FLAGGED if kind == "security" else STATUS_AWAITING_REVIEW, "resync")
            elif status == "failed":
                runner.start(cid, None)  # None = continue from the last checkpoint
                log.info("Resync: resumed claim check for %s", cid)
        except Exception:  # noqa: BLE001 - one broken claim must not stop the API
            log.exception("Resync failed for claim %s", cid)
