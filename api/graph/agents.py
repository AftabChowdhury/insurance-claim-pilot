"""Specialist agents: intake, coverage, fraud, letter writer, evaluator (+ retry preparation)."""
import json
from datetime import date

from core import db
from core.config import get_settings
from core.domain import label_for, money
from core.llm import structured

from ..mcp_client import call_tool_or_fallback
from . import prompts
from .evidence import SIGNALS, evidence_for_prompt
from .schemas import ClaimFacts, CoverageDecision, DecisionLetter, FraudAssessment, Judgement
from .state import ClaimState, event

MAX_DOC_CHARS = 6000


def _docs_for_prompt(state: ClaimState) -> str:
    parts = []
    for d in state.get("clean_documents", []):
        if d.get("text"):
            parts.append(f"### {d['name']}\n{d['text'][:MAX_DOC_CHARS]}")
        else:
            parts.append(f"### {d['name']}\n(no text extracted: {d.get('extraction')})")
    return "\n\n".join(parts) or "(no documents)"


# ---------------------------------------------------------------------------
async def intake_agent(state: ClaimState) -> dict:
    facts: ClaimFacts = await structured(ClaimFacts, mini=True).ainvoke(prompts.INTAKE.format(
        policy_number=state["policy_number"], loss_type=label_for(state["loss_type"]), amount=state["amount"],
        loss_date=state.get("loss_date", ""), description=state.get("clean_description", ""),
        documents=_docs_for_prompt(state)))
    claim = {"policy_number": state["policy_number"], "loss_type": label_for(state["loss_type"]), **facts.model_dump()}
    return {"claim": claim, "retries": 0,
            **event("intake_agent", facts.loss_summary, cause=facts.cause_of_loss, missing=facts.missing_information)}


# ---------------------------------------------------------------------------
async def coverage_agent(state: ClaimState) -> dict:
    out: CoverageDecision = await structured(CoverageDecision).ainvoke(prompts.COVERAGE.format(
        claim=json.dumps(state["claim"], indent=2), evidence=evidence_for_prompt(state), context=state["context"]))
    cov = out.model_dump()
    if out.covered == "yes":
        msg = f"Covered under {', '.join(out.applicable_clauses) or '?'} · payable {money(out.payable_amount)}"
    elif out.covered == "no":
        msg = f"Not covered · exclusions {', '.join(out.exclusions_applied) or '—'}"
    else:
        msg = "Coverage unclear from retrieved clauses"
    return {"coverage": cov, **event("coverage_agent", msg, "done" if out.covered != "unclear" else "warn", **cov)}


# ---------------------------------------------------------------------------
def _rule_fraud_score(state: ClaimState, history: list[dict], watch: list[str]) -> tuple[float, list[str]]:
    score, signals = 0.05, []
    amount = float(state.get("amount") or 0)
    if amount > 50_000:
        score += 0.3; signals.append(f"High claim amount (> {money(50_000)})")
    elif amount > 20_000:
        score += 0.1; signals.append(f"Claim amount above {money(20_000)}")
    recent = [h for h in history if (h.get("loss_date") or "") >= f"{date.today().year - 2}"]
    if len(recent) >= 3:
        score += 0.25; signals.append(f"{len(recent)} claims in the last 2 years")
    if watch:
        score += 0.25; signals += watch
    if len(state.get("documents", [])) < 2:
        score += 0.1; signals.append("Fewer than 2 supporting documents")
    # Image evidence signals (verify_evidence): each signal type counts once, with its weight.
    codes = (state.get("evidence_summary") or {}).get("signal_codes", [])
    if codes:
        score += sum(SIGNALS[c][0] for c in codes if c in SIGNALS)
        signals += [f"Evidence: {x}" for x in (state.get("evidence_summary") or {}).get("signals", [])[:5]]
    return min(score, 0.95), signals


async def fraud_agent(state: ClaimState) -> dict:
    policy = state["policy_number"]
    history, via_mcp_h = await call_tool_or_fallback(
        "fraud", "check_prior_claims", {"policy_number": policy, "exclude_claim_id": state["claim_id"]},
        lambda: {"claims": db.claim_history(policy, state["claim_id"])})
    watch, via_mcp_w = await call_tool_or_fallback(
        "fraud", "check_watchlist", {"policy_number": policy},
        lambda: {"signals": []})
    history_list = history.get("claims", []) if isinstance(history, dict) else []
    watch_list = watch.get("signals", []) if isinstance(watch, dict) else []

    rule_score, rule_signals = _rule_fraud_score(state, history_list, watch_list)
    out: FraudAssessment = await structured(FraudAssessment, mini=True).ainvoke(prompts.FRAUD.format(
        rule_score=round(rule_score, 2), claim=json.dumps(state["claim"], indent=2),
        history=json.dumps(history_list, indent=2) or "none", watchlist=json.dumps(watch_list) or "none",
        evidence=evidence_for_prompt(state)))

    risk = round(max(rule_score, out.risk_score), 2)  # never lower than the deterministic rules
    signals = list(dict.fromkeys(rule_signals + out.signals))
    level = "high" if risk >= get_settings().fraud_review_threshold else "medium" if risk >= 0.3 else "low"
    fraud = {"risk_score": risk, "level": level, "signals": signals, "rationale": out.rationale,
             "rule_score": round(rule_score, 2), "llm_score": out.risk_score, "prior_claims": len(history_list),
             "via_mcp": via_mcp_h and via_mcp_w}
    return {"fraud": fraud,
            **event("fraud_agent", f"Fraud risk {risk:.2f} ({level})" + (" · via MCP" if fraud["via_mcp"] else " · MCP offline, local fallback"),
                    "warn" if level == "high" else "done", **fraud)}


# ---------------------------------------------------------------------------
async def writer_agent(state: ClaimState) -> dict:
    out: DecisionLetter = await structured(DecisionLetter).ainvoke(prompts.WRITER.format(
        claim_id=state["claim_id"], claim=json.dumps(state["claim"], indent=2),
        coverage=json.dumps(state["coverage"], indent=2),
        context=state.get("public_context") or state["context"]))
    return {"letter": out.model_dump(),
            **event("writer_agent", f"Draft: {out.decision} · {money(out.payout)} · {len(out.citations)} citation(s)",
                    decision=out.decision, payout=out.payout)}


# ---------------------------------------------------------------------------
async def evaluator_agent(state: ClaimState) -> dict:
    s = get_settings()
    letter = state["letter"]
    out: Judgement = await structured(Judgement, mini=True).ainvoke(prompts.JUDGE.format(
        claim=json.dumps(state["claim"], indent=2), context=state["context"],
        decision=letter["decision"], payout=letter["payout"], letter=letter["letter"]))
    j = out.model_dump()

    # Deterministic check on top of the judge: citations must exist in the retrieved context.
    known = set(state.get("context_clauses", []))
    bad = [c for c in letter.get("citations", []) if c not in known]
    if bad:
        j["citation_coverage"] = min(j["citation_coverage"], 0.5)
        j["unsupported_statements"].append(f"Cited clauses not in context: {', '.join(bad)}")

    passed = j["faithfulness"] >= s.eval_threshold and j["citation_coverage"] >= 0.7 and j["answer_relevance"] >= 0.6
    j["passed"] = passed
    j["threshold"] = s.eval_threshold
    j["attempt"] = state.get("retries", 0) + 1
    msg = f"faithfulness {j['faithfulness']:.2f} · citations {j['citation_coverage']:.2f} → {'PASS' if passed else 'FAIL'}"
    return {"evaluation": j, **event("evaluator_agent", msg, "done" if passed else "fail", **j)}


def route_after_eval(state: ClaimState) -> str:
    if state["evaluation"].get("passed"):
        return "guard_output"
    if state.get("retries", 0) < get_settings().max_retries:
        return "prepare_retry"
    return "guard_output"  # out of retries -> guard_output escalates to a human


async def prepare_retry(state: ClaimState) -> dict:
    """Self-correction: feed the judge's feedback into the next query rewrite and widen retrieval."""
    s = get_settings()
    retries = state.get("retries", 0) + 1
    ev = state["evaluation"]
    feedback = ev.get("feedback", "")
    if ev.get("missing_considerations"):
        feedback += " Missing: " + "; ".join(ev["missing_considerations"])
    top_k = (state.get("top_k") or s.retrieval_top_k) + 3
    min_score = max(0.5, state.get("min_reranker_score", s.min_reranker_score) - 0.5)
    return {"retries": retries, "feedback": feedback, "top_k": top_k, "min_reranker_score": min_score,
            **event("prepare_retry", f"Retry {retries}/{s.max_retries}: rewrite queries, top_k → {top_k}", "warn",
                    feedback=feedback)}
