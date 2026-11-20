"""Input guardrails (prompt injection, PII masking) and output guardrails (letter policy + decision rules)."""
import asyncio
import logging
import re
import unicodedata

from core import db
from core.config import get_settings
from core.domain import STATUS_AWAITING_REVIEW, STATUS_FLAGGED, money
from core.llm import structured

from . import prompts
from .schemas import InjectionCheck
from .state import ClaimState, event

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------
INJECTION_RE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+instructions"
    r"|approve\s+(a\s+|the\s+)?full\s+payout"
    r"|you\s+are\s+now\s+"
    r"|(reveal|print|show)\s+(your\s+)?system\s+prompt"
    r"|<\s*/?\s*(system|assistant)\s*>",
    re.IGNORECASE,
)
# Extra patterns for claims only (the policy chat keeps INJECTION_RE): instructions addressed to an AI / the system,
# role markers, payout manipulation, chat-template tokens and long encoded blobs. Normal requests such as
# "please approve my claim" do not match.
CLAIM_INJECTION_RE = re.compile(
    INJECTION_RE.pattern
    + r"|\b(ai|a\.i\.|assistant|chatbot|llm|language\s+model|agent|claims?\s+system|system)\b[\s,:;\-]{1,5}"
      r"(you\s+)?(must\s+|should\s+|shall\s+|please\s+)?(approve|accept|pay|set|override|ignore|disregard|skip)\b"
    + r"|\bset\s+(the\s+)?payout\s+(to|=)"
    + r"|\b(ignore|disregard|skip|bypass)\s+(the\s+|all\s+|any\s+)?(policy|policies|exclusions?|rules|checks|"
      r"fraud\s+checks?|guardrails?)\b"
    + r"|\boverride\s+(the\s+)?(decision|policy|rules|assessment)"
    + r"|\bnew\s+instructions\s*:"
    + r"|(^|\n)\s*(system|assistant|developer)\s*:"
    + r"|<\|im_(start|end)\|>|\[/?(inst|sys)\]"
    + r"|[A-Za-z0-9+/]{200,}={0,2}",
    re.IGNORECASE,
)
# Invisible / direction-changing characters used to hide instructions or split trigger words.
_INVISIBLE = re.compile("[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]")
LLM_INJECTION_THRESHOLD = 0.8
MAX_CHECK_CHARS = 8000


def normalize_text(text: str) -> str:
    """Canonical form for the injection checks: NFKC (full-width / ligature tricks), invisible characters removed,
    spaces collapsed (line breaks kept for the role-marker pattern)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text)


async def _llm_injection_check(source: str, text: str) -> InjectionCheck | None:
    try:
        return await structured(InjectionCheck, mini=True).ainvoke(
            prompts.INJECTION.format(source=source, text=text[:MAX_CHECK_CHARS]))
    except Exception as e:  # noqa: BLE001 - never let the classifier crash the pipeline
        log.warning("LLM injection check failed for %s: %s", source, e)
        return None


# ---------------------------------------------------------------------------
# PII masking: Presidio if installed, regex fallback otherwise
# ---------------------------------------------------------------------------
PII_PATTERNS = {
    "EMAIL_ADDRESS": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "PHONE_NUMBER": re.compile(r"\+?\d[\d\s().-]{8,}\d"),
    "IBAN_CODE": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
}

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    _nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
    _analyzer = AnalyzerEngine(nlp_engine=_nlp)
    _anonymizer = AnonymizerEngine()
    PII_ENGINE = "presidio"
except Exception:  # noqa: BLE001 - presidio or the spaCy model is optional
    _analyzer = _anonymizer = None
    PII_ENGINE = "regex"

PRESIDIO_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "US_SSN", "UK_NHS"]


def mask_pii(text: str) -> tuple[str, list[str]]:
    if not text:
        return text, []
    if _analyzer:
        results = _analyzer.analyze(text=text, language="en", entities=PRESIDIO_ENTITIES)
        masked = _anonymizer.anonymize(text=text, analyzer_results=results).text
        return masked, [r.entity_type for r in results]
    found = []
    for name, pattern in PII_PATTERNS.items():
        text, n = pattern.subn(f"<{name}>", text)
        found += [name] * n
    return text, found


# ---------------------------------------------------------------------------
# Node: input guardrails
# ---------------------------------------------------------------------------
WITHHELD = "[withheld: flagged by the security filter]"


def _clean_inputs(state: ClaimState, flagged: str | None = None) -> tuple[str, list[dict], list[str]]:
    """PII-masked description + documents for the agents. A flagged source is withheld (used if an admin releases
    a security-flagged claim for the normal check)."""
    desc = WITHHELD if flagged == "claim description" else state.get("description", "")
    clean_desc, pii = mask_pii(desc)
    clean_docs = []
    for i, d in enumerate(state.get("documents", []), 1):
        if flagged in (d["name"], f"file name {d['name']}"):
            clean_docs.append({"name": f"document {i}", "extraction": d.get("extraction"), "text": WITHHELD})
            continue
        masked, found = mask_pii(d.get("text") or "")
        pii += found
        clean_docs.append({"name": d["name"], "extraction": d.get("extraction"), "text": masked or None})
    return clean_desc, clean_docs, pii


def _blocked(state: ClaimState, method: str, source: str, excerpt: str, message: str, **extra) -> dict:
    """Stop the automatic check: the claim goes to the admin's security review, never to the agents."""
    db.set_status(state["claim_id"], STATUS_FLAGGED, "guardrail", f"{method}: {source}")
    clean_desc, clean_docs, _ = _clean_inputs(state, flagged=source)
    guard = {"verdict": "BLOCK", "method": method, "source": source, "excerpt": (excerpt or "")[:300], **extra}
    return {"blocked": True, "guard_in": guard, "clean_description": clean_desc, "clean_documents": clean_docs,
            **event("guard_input", message, "fail", source=source, excerpt=guard["excerpt"])}


async def guard_input(state: ClaimState) -> dict:
    # 0) text inside an image that addresses an AI (read by the vision model in verify_evidence)
    for inj in state.get("evidence_injection", []):
        return _blocked(state, "image", inj["source"], inj["excerpt"],
                        f"Instructions to an AI found inside image {inj['source']}")

    sources = [("claim description", state.get("description", ""))]
    sources += [(f"file name {d['name']}", d["name"]) for d in state.get("documents", [])]
    sources += [(d["name"], d["text"]) for d in state.get("documents", []) if d.get("text")]
    sources = [(n, normalize_text(t)) for n, t in sources]

    # 1) fast regex screen on the normalised text
    for name, text in sources:
        m = CLAIM_INJECTION_RE.search(text or "")
        if m:
            start = max(0, m.start() - 40)
            return _blocked(state, "pattern", name, text[start:m.end() + 60].strip(),
                            f"Prompt injection detected in {name}")

    # 2) LLM classifier on all text sources (not the file names), in parallel
    text_sources = [(n, t) for n, t in sources if t and not n.startswith("file name ")]
    checks = await asyncio.gather(*[_llm_injection_check(n, t) for n, t in text_sources])
    for (name, _), chk in zip(text_sources, checks):
        if chk and chk.is_injection and chk.score >= LLM_INJECTION_THRESHOLD:
            return _blocked(state, "llm", name, chk.evidence,
                            f"Prompt injection detected in {name} (score {chk.score:.2f})", score=chk.score)

    # 3) PII masking
    clean_desc, clean_docs, pii = _clean_inputs(state)
    no_text = [d["name"] for d in state.get("documents", []) if d.get("extraction") in ("ocr_pending", "failed")]
    summary = {"verdict": "PASS", "pii_engine": PII_ENGINE, "pii_masked": len(pii), "pii_types": sorted(set(pii)),
               "scanned_sources": [n for n, _ in text_sources], "no_text": no_text}
    msg = f"{len(text_sources)} source(s) + file names scanned · {len(pii)} PII item(s) masked · no injection"
    if no_text:
        msg += f" · {len(no_text)} file(s) without readable text"
    return {"blocked": False, "guard_in": summary, "clean_description": clean_desc, "clean_documents": clean_docs,
            **event("guard_input", msg, **summary)}


def route_after_input(state: ClaimState) -> str:
    return "human_review" if state.get("blocked") else "intake_agent"


# ---------------------------------------------------------------------------
# Node: output guardrails + decision rules
# ---------------------------------------------------------------------------
BANNED = re.compile(r"\b(we guarantee|guaranteed payment|legal advice|you should sue|lawsuit)\b", re.IGNORECASE)
APPEAL = re.compile(r"\bappeal", re.IGNORECASE)
CITATION = re.compile(r"\[Clause\s+([A-Z]?-?\d+(?:\.\d+)?)\]", re.IGNORECASE)
# Internal information that must never reach the customer letter.
INTERNAL = re.compile(r"\b(fraud|risk[\s-]score|watch\s?list|system\s+prompt|guardrails?|prompt\s+injection|"
                      r"exif|metadata|perceptual\s+hash|confidential|internal\s+(?:notes?|clauses?|guidelines?)|"
                      r"language\s+model|as\s+an\s+ai)\b", re.IGNORECASE)
NEED_EVIDENCE_REASON = ("We could not assess your claim because the uploaded files are not readable. Please upload "
                        "clear photos of the damage and the invoice or receipt.")


def _need_evidence_letter(state: ClaimState) -> dict:
    text = (f"Dear customer,\n\nthank you for your claim {state['claim_id']}. {NEED_EVIDENCE_REASON} "
            "As soon as we have them, we will continue the assessment.\n\nKind regards,\nYour claims team")
    return {"decision": "need_info", "payout": 0.0, "reason": NEED_EVIDENCE_REASON, "letter": text, "citations": []}


async def guard_output(state: ClaimState) -> dict:
    s = get_settings()
    letter = dict(state.get("letter", {}))
    coverage = state.get("coverage", {})
    fraud = state.get("fraud", {})
    evidence = state.get("evidence_summary") or {}
    evaluation = state.get("evaluation", {})
    text = letter.get("letter", "")
    decision = letter.get("decision")
    payout = float(letter.get("payout") or 0)
    amount = float(state.get("amount") or 0)
    known = set(state.get("context_clauses", []))
    usable = evidence.get("has_usable_evidence", True)

    violations = []
    cited = set(CITATION.findall(text)) | set(letter.get("citations", []))
    if decision in ("approve", "deny") and not cited:
        violations.append("Decision letter has no clause citations")
    unknown = sorted(c for c in cited if known and c not in known)
    if unknown:
        violations.append(f"Cites clauses not in the retrieved context: {', '.join(unknown)}")
    confidential = sorted(c for c in cited if c in set(state.get("confidential_clauses", [])))
    if confidential:
        violations.append(f"Letter cites confidential (internal) clauses: {', '.join(confidential)}")
    # Decision integrity: the letter may not contradict the coverage analysis or the documented amounts.
    if decision == "approve" and coverage.get("covered") != "yes":
        violations.append(f"Letter approves although coverage is '{coverage.get('covered')}'")
    if decision == "deny" and coverage.get("covered") == "yes":
        violations.append("Letter denies although the coverage analyst found the claim covered")
    if decision != "approve" and payout > 0:
        violations.append("Payout given without an approval")
    if payout > amount + 0.01:
        violations.append("Payout exceeds the claimed amount")
    if coverage.get("policy_limit") and payout > float(coverage["policy_limit"]) + 0.01:
        violations.append("Payout exceeds the policy limit")
    if evidence.get("documented_total") and payout > float(evidence["documented_total"]) + 0.01:
        violations.append(f"Payout exceeds the documented invoice total {money(evidence['documented_total'])}")
    if decision == "approve" and abs(payout - float(coverage.get("payable_amount") or 0)) > 1:
        violations.append("Payout differs from the coverage analyst's calculation")
    if decision == "deny" and not APPEAL.search(text):
        violations.append("Denial letter does not mention appeal rights")
    if BANNED.search(text):
        violations.append("Letter contains prohibited wording (guarantees / legal advice)")
    if m := INTERNAL.search(f"{text} {letter.get('reason', '')}"):
        violations.append(f"Letter mentions internal information ('{m.group(0)}')")

    reasons = []
    if state.get("security_released"):
        reasons.append("Released by an admin after a security flag – the final decision is the admin's")
    if amount > s.hitl_payout_threshold:
        reasons.append(f"Claimed amount {money(amount)} exceeds {money(s.hitl_payout_threshold)}")
    if payout > s.hitl_payout_threshold:
        reasons.append(f"Payout {money(payout)} exceeds {money(s.hitl_payout_threshold)}")
    if float(fraud.get("risk_score") or 0) >= s.fraud_review_threshold:
        reasons.append(f"Fraud risk {fraud['risk_score']:.2f} ≥ {s.fraud_review_threshold}")
    if evidence.get("suspicious"):
        reasons.append(f"{evidence['suspicious']} suspicious image(s): " + "; ".join(evidence.get("signals", [])[:3]))
    if evidence.get("unverified"):
        reasons.append(f"{evidence['unverified']} image(s) could not be analysed (vision model unavailable)")
    if usable and not evaluation.get("passed", False):
        reasons.append(f"Evaluation still failing after {state.get('retries', 0)} retries")
    if usable and coverage.get("covered") == "unclear":
        reasons.append("Coverage unclear")
    if usable:
        reasons += violations

    # Decision rules in code (never by the LLM): any reason -> admin; no usable evidence -> ask for it;
    # otherwise the (checked) letter decision is applied automatically.
    if reasons:
        auto = "review"
    elif not usable:
        auto, letter = "need_info", _need_evidence_letter(state)
    elif decision in ("approve", "deny", "need_info"):
        auto = decision
    else:
        auto = "review"
        reasons.append(f"Unknown decision '{decision}'")

    needs_review = auto == "review"
    if needs_review:
        db.set_status(state["claim_id"], STATUS_AWAITING_REVIEW, "claimpilot", "; ".join(reasons)[:500])

    result = {"violations": violations, "escalate": reasons, "auto_decision": auto}
    msg = ("Output checks passed" if not violations else f"{len(violations)} violation(s)")
    msg += " · escalate to admin" if needs_review else f" · automatic decision: {auto}"
    return {"guard_out": result, "needs_review": needs_review, "review_reasons": reasons, "auto_decision": auto,
            "letter": letter, **event("guard_output", msg, "warn" if needs_review else "done", **result)}


def route_after_output(state: ClaimState) -> str:
    return "human_review" if state.get("needs_review") else "close_claim"
