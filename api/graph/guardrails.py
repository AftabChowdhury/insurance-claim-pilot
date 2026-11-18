"""Input guardrails (PII masking, prompt injection) and output guardrails (policy + format checks)."""
import asyncio
import logging
import re

from core.config import get_settings
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
LLM_INJECTION_THRESHOLD = 0.8
MAX_CHECK_CHARS = 8000


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
async def guard_input(state: ClaimState) -> dict:
    sources = [("claim description", state.get("description", ""))]
    sources += [(d["name"], d["text"]) for d in state.get("documents", []) if d.get("text")]

    # 1) fast regex screen
    for name, text in sources:
        m = INJECTION_RE.search(text or "")
        if m:
            start = max(0, m.start() - 40)
            excerpt = text[start:m.end() + 60].strip()
            return {
                "blocked": True,
                "guard_in": {"verdict": "BLOCK", "method": "pattern", "source": name, "excerpt": excerpt},
                **event("guard_input", f"Prompt injection detected in {name}", "fail", source=name, excerpt=excerpt),
            }

    # 2) LLM classifier, all sources in parallel
    checks = await asyncio.gather(*[_llm_injection_check(n, t) for n, t in sources if t])
    for (name, _), chk in zip([s for s in sources if s[1]], checks):
        if chk and chk.is_injection and chk.score >= LLM_INJECTION_THRESHOLD:
            return {
                "blocked": True,
                "guard_in": {"verdict": "BLOCK", "method": "llm", "source": name, "score": chk.score, "excerpt": chk.evidence},
                **event("guard_input", f"Prompt injection detected in {name} (score {chk.score:.2f})", "fail",
                        source=name, excerpt=chk.evidence),
            }

    # 3) PII masking
    clean_desc, pii = mask_pii(state.get("description", ""))
    clean_docs = []
    for d in state.get("documents", []):
        masked, found = mask_pii(d.get("text") or "")
        pii += found
        clean_docs.append({"name": d["name"], "extraction": d.get("extraction"), "text": masked or None})

    ocr_pending = [d["name"] for d in state.get("documents", []) if d.get("extraction") == "ocr_pending"]
    summary = {"verdict": "PASS", "pii_engine": PII_ENGINE, "pii_masked": len(pii), "pii_types": sorted(set(pii)),
               "scanned_sources": [n for n, t in sources if t], "ocr_pending": ocr_pending}
    msg = f"{len(summary['scanned_sources'])} source(s) scanned · {len(pii)} PII item(s) masked · no injection"
    if ocr_pending:
        msg += f" · {len(ocr_pending)} file(s) need OCR"
    return {"blocked": False, "guard_in": summary, "clean_description": clean_desc, "clean_documents": clean_docs,
            **event("guard_input", msg, **summary)}


def route_after_input(state: ClaimState) -> str:
    return "close_claim" if state.get("blocked") else "intake_agent"


# ---------------------------------------------------------------------------
# Node: output guardrails + escalation rules
# ---------------------------------------------------------------------------
BANNED = re.compile(r"\b(we guarantee|guaranteed payment|legal advice|you should sue|lawsuit)\b", re.IGNORECASE)
APPEAL = re.compile(r"\bappeal", re.IGNORECASE)
CITATION = re.compile(r"\[Clause\s+([A-Z]?-?\d+(?:\.\d+)?)\]", re.IGNORECASE)


async def guard_output(state: ClaimState) -> dict:
    s = get_settings()
    letter = state.get("letter", {})
    coverage = state.get("coverage", {})
    fraud = state.get("fraud", {})
    evaluation = state.get("evaluation", {})
    text = letter.get("letter", "")
    decision = letter.get("decision")
    payout = float(letter.get("payout") or 0)
    known = set(state.get("context_clauses", []))

    violations = []
    cited = set(CITATION.findall(text)) | set(letter.get("citations", []))
    if decision in ("approve", "deny") and not cited:
        violations.append("Decision letter has no clause citations")
    unknown = sorted(c for c in cited if known and c not in known)
    if unknown:
        violations.append(f"Cites clauses not in the retrieved context: {', '.join(unknown)}")
    if payout > float(state.get("amount") or 0) + 0.01:
        violations.append("Payout exceeds the claimed amount")
    if coverage.get("policy_limit") and payout > float(coverage["policy_limit"]) + 0.01:
        violations.append("Payout exceeds the policy limit")
    if decision == "approve" and abs(payout - float(coverage.get("payable_amount") or 0)) > 1:
        violations.append("Payout differs from the coverage analyst's calculation")
    if decision == "deny" and not APPEAL.search(text):
        violations.append("Denial letter does not mention appeal rights")
    if BANNED.search(text):
        violations.append("Letter contains prohibited wording (guarantees / legal advice)")

    reasons = []
    if payout > s.hitl_payout_threshold:
        reasons.append(f"Payout ${payout:,.0f} exceeds ${s.hitl_payout_threshold:,.0f}")
    if float(fraud.get("risk_score") or 0) >= s.fraud_review_threshold:
        reasons.append(f"Fraud risk {fraud['risk_score']:.2f} ≥ {s.fraud_review_threshold}")
    if not evaluation.get("passed", False):
        reasons.append(f"Evaluation still failing after {state.get('retries', 0)} retries")
    if coverage.get("covered") == "unclear":
        reasons.append("Coverage unclear")
    reasons += violations

    result = {"violations": violations, "escalate": reasons}
    status = "warn" if reasons else "done"
    msg = "Output checks passed" if not violations else f"{len(violations)} violation(s)"
    if reasons:
        msg += " · escalate to adjuster"
    return {"guard_out": result, "needs_review": bool(reasons), "review_reasons": reasons,
            **event("guard_output", msg, status, **result)}


def route_after_output(state: ClaimState) -> str:
    return "human_review" if state.get("needs_review") else "close_claim"
