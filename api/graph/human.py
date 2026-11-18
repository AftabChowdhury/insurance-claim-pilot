"""Human-in-the-loop review and the final close-out step."""
from langgraph.types import interrupt

from core import db
from core.domain import DECISION_TO_STATUS, STATUS_ESCALATED, STATUS_FLAGGED

from ..mcp_client import call_tool_or_fallback
from .state import ClaimState, event


async def human_review(state: ClaimState) -> dict:
    """Pauses the graph. The API resumes it with Command(resume={"action": ..., "letter": ..., "reviewer": ...}).
    NOTE: on resume this node runs again from the top, so nothing with side effects goes before interrupt()."""
    letter = state.get("letter", {})
    answer = interrupt({
        "claim_id": state["claim_id"],
        "reasons": state.get("review_reasons", []),
        "decision": letter.get("decision"),
        "payout": letter.get("payout"),
        "draft_letter": letter.get("letter", ""),
        "fraud": state.get("fraud", {}),
        "evaluation": state.get("evaluation", {}),
    })

    action = answer.get("action", "approve")
    reviewer = answer.get("reviewer", "adjuster")
    new_letter = dict(letter)
    if action in ("approve", "edit") and answer.get("letter"):
        if answer["letter"].strip() != letter.get("letter", "").strip():
            action = "edit"
        new_letter["letter"] = answer["letter"]
    if answer.get("decision") in ("approve", "deny", "need_info"):
        new_letter["decision"] = answer["decision"]
    if answer.get("payout") is not None:
        new_letter["payout"] = float(answer["payout"])

    human = {"action": action, "reviewer": reviewer, "note": answer.get("note", "")}
    db.add_audit(state["claim_id"], reviewer, f"review:{action}", answer.get("note", ""))
    return {"human": human, "letter": new_letter,
            **event("human_review", f"{reviewer}: {action}", "fail" if action == "reject" else "done", **human)}


async def close_claim(state: ClaimState) -> dict:
    claim_id = state["claim_id"]
    letter = state.get("letter", {})
    human = state.get("human")

    if state.get("blocked"):
        status, payout, by = STATUS_FLAGGED, 0.0, "guardrail"
        text = ""
    elif human and human["action"] == "reject":
        status, payout, by = STATUS_ESCALATED, 0.0, human["reviewer"]
        text = ""
    else:
        status = DECISION_TO_STATUS.get(letter.get("decision"), STATUS_ESCALATED)
        payout = float(letter.get("payout") or 0) if status == "APPROVED" else 0.0
        by = human["reviewer"] if human else "claimpilot"
        text = letter.get("letter", "")

    _, via_mcp = await call_tool_or_fallback(
        "claims", "update_claim_status",
        {"claim_id": claim_id, "status": status, "payout": payout, "decided_by": by},
        lambda: db.update_status(claim_id, status, payout, by))
    if text:
        await call_tool_or_fallback("claims", "save_decision_letter", {"claim_id": claim_id, "letter": text},
                                    lambda: db.save_letter(claim_id, text))

    final = {"status": status, "payout": payout, "decided_by": by, "via_mcp": via_mcp}
    return {"final": final,
            **event("close_claim", f"status → {status}" + (f" · payout ${payout:,.0f}" if payout else "")
                    + (" · via claims-mcp" if via_mcp else " · MCP offline, saved locally"), final=final)}
