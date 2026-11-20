"""Human-in-the-loop review (admin Accept / Reject) and the final close-out step."""
from langgraph.types import interrupt

from core import db
from core.domain import DECISION_TO_STATUS, STATUS_DENIED, STATUS_ESCALATED, money

from ..mcp_client import call_tool_or_fallback
from .state import ClaimState, event

REJECT_ACTIONS = {"reject"}
ACCEPT_ACTIONS = {"accept", "approve", "edit"}


def _acceptance_letter(claim_id: str, payout: float, reason: str) -> str:
    return (f"Dear customer,\n\nafter reviewing your claim {claim_id}, we are pleased to approve it with a payout "
            f"of {money(payout)}.\n\n" + (f"{reason}\n\n" if reason else "")
            + "The amount will be transferred to your account on file.\n\nKind regards,\nYour claims team")


def _rejection_letter(claim_id: str, reason: str) -> str:
    return (f"Dear customer,\n\nafter reviewing your claim {claim_id}, we are unable to accept it.\n\n"
            f"Reason: {reason}\n\nYou may appeal this decision within 60 days by replying with any additional "
            "information or documents.\n\nKind regards,\nYour claims team")


async def human_review(state: ClaimState) -> dict:
    """Pauses the graph until an admin decides. The API resumes it with
    Command(resume={"action": "accept" | "reject" | "release", "reason", "payout", "letter", "reviewer", "note"}).

    kind "security": the input guardrail blocked the claim (prompt attack). The admin can Reject it, or Release it
    for the normal check with the flagged content withheld (the claim then comes back for a final admin decision).
    kind "decision": the claim check finished but needs a human (amount > threshold, fraud, suspicious evidence ...).

    NOTE: on resume this node runs again from the top, so nothing with side effects goes before interrupt()."""
    security = bool(state.get("blocked"))
    letter = state.get("letter", {})
    answer = interrupt({
        "kind": "security" if security else "decision",
        "claim_id": state["claim_id"],
        "reasons": ([f"Security: {state.get('guard_in', {}).get('source')} – possible prompt injection"]
                    if security else state.get("review_reasons", [])),
        "guard": state.get("guard_in", {}) if security else None,
        "decision": letter.get("decision"),
        "payout": letter.get("payout"),
        "reason": letter.get("reason", ""),
        "draft_letter": letter.get("letter", ""),
        "fraud": state.get("fraud", {}),
        "evidence": state.get("evidence_summary", {}),
        "evaluation": state.get("evaluation", {}),
    })

    action = answer.get("action", "accept")
    action = "accept" if action in ACCEPT_ACTIONS else action
    reviewer = answer.get("reviewer", "admin")
    note = answer.get("note", "")
    reason = (answer.get("reason") or "").strip()
    db.add_audit(state["claim_id"], reviewer, f"review:{action}", reason or note)

    if security and action == "release":
        human = {"action": "release", "reviewer": reviewer, "note": note}
        return {"human": human, "blocked": False, "security_released": True,
                **event("human_review", f"{reviewer}: released for the claim check (flagged content withheld)",
                        "warn", **human)}

    new_letter = dict(letter)
    if action == "accept":
        if security:  # accepting a blocked claim without a check is not allowed: treat as release
            human = {"action": "release", "reviewer": reviewer, "note": note}
            return {"human": human, "blocked": False, "security_released": True,
                    **event("human_review", f"{reviewer}: released for the claim check", "warn", **human)}
        payout = float(answer["payout"]) if answer.get("payout") is not None else float(letter.get("payout") or 0)
        edited = (answer.get("letter") or "").strip()
        draft_fits = letter.get("decision") == "approve" and abs(payout - float(letter.get("payout") or 0)) < 0.01
        new_letter["reason"] = (reason or (letter.get("reason") if draft_fits else "")
                                or "Approved after review by a claims adjuster.")
        if edited and (edited != (letter.get("letter") or "").strip() or draft_fits):
            new_letter["letter"] = edited
        elif not draft_fits:  # the agents' draft was a denial / need_info or had another amount
            new_letter["letter"] = _acceptance_letter(state["claim_id"], payout, reason)
        new_letter.update(decision="approve", payout=payout)
    else:  # reject
        new_letter.update(decision="deny", payout=0.0, reason=reason or "Declined after review.",
                          letter=(answer.get("letter") or "").strip() or _rejection_letter(state["claim_id"], reason))

    human = {"action": action, "reviewer": reviewer, "note": note, "reason": reason}
    msg = f"{reviewer}: {action}" + (f" · {money(new_letter.get('payout'))}" if action == "accept" else f" · {reason}")
    return {"human": human, "letter": new_letter,
            **event("human_review", msg, "fail" if action == "reject" else "done", **human)}


def route_after_human(state: ClaimState) -> str:
    """Released security claims run the normal check; every other review closes the claim."""
    return "intake_agent" if (state.get("human") or {}).get("action") == "release" else "close_claim"


async def close_claim(state: ClaimState) -> dict:
    claim_id = state["claim_id"]
    letter = state.get("letter", {})
    human = state.get("human")

    if human and human["action"] == "reject":
        status, payout, by = STATUS_DENIED, 0.0, human["reviewer"]
    else:
        status = DECISION_TO_STATUS.get(letter.get("decision"), STATUS_ESCALATED)
        payout = float(letter.get("payout") or 0) if status == "APPROVED" else 0.0
        by = human["reviewer"] if human else "claimpilot"
    text = letter.get("letter", "")
    reason = letter.get("reason", "")

    _, via_mcp = await call_tool_or_fallback(
        "claims", "update_claim_status",
        {"claim_id": claim_id, "status": status, "payout": payout, "decided_by": by, "note": reason[:300]},
        lambda: db.update_status(claim_id, status, payout, by, reason[:300]))
    if text:
        await call_tool_or_fallback("claims", "save_decision_letter", {"claim_id": claim_id, "letter": text},
                                    lambda: db.save_letter(claim_id, text))
    db.save_decision_reason(claim_id, reason)

    final = {"status": status, "payout": payout, "decided_by": by, "reason": reason, "via_mcp": via_mcp,
             "automatic": not human}
    return {"final": final,
            **event("close_claim", f"status → {status}" + (f" · payout {money(payout)}" if payout else "")
                    + (" · automatic" if not human else f" · by {by}")
                    + (" · via claims-mcp" if via_mcp else " · MCP offline, saved locally"), final=final)}
