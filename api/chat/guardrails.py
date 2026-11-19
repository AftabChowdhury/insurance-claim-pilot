"""Input and output guardrails of the policy chat.

Input  (Input Guardrail Agent): prompt injection + confidentiality-bypass attempts (regex, then mini LLM),
                                PII masking of the message.
Output (Output Guardrail Agent): PII masking, banned promises, citation validity, and a confidential-leak
                                 backstop: 12-word shingles of the answer vs hashed shingles of the confidential
                                 chunks the probe found (no confidential text is ever kept in the state).
"""
import hashlib
import logging
import re

from api.graph.guardrails import CITATION, INJECTION_RE, LLM_INJECTION_THRESHOLD, PII_ENGINE, mask_pii
from api.graph.schemas import InjectionCheck
from core.llm import structured

from . import prompts
from .state import ChatState, event

log = logging.getLogger(__name__)

BYPASS_RE = re.compile(
    r"\b(ignore|bypass|override|disable|skip|turn\s+off)\s+(the\s+|all\s+|any\s+|your\s+)?"
    r"(confidential\w*|restrictions?|access\s+(control|rules?)|security|filters?|guardrails?)"
    r"|\b(show|print|reveal|list|give|send|dump|quote|display|output)\s+(me\s+)?(all\s+|the\s+|every\s+|any\s+)*"
    r"(confidential|restricted|internal|secret|hidden|private)\s+"
    r"(documents?|polic(y|ies)|files?|guidelines?|content|information|info|text|clauses?|data)"
    r"|\blist\s+(all\s+)?(the\s+)?(documents|files)\b"
    r"|\b(contents?|dump)\s+of\s+(the\s+)?(search\s+)?index\b",
    re.IGNORECASE,
)
BANNED = re.compile(
    r"\b(we guarantee|guaranteed (payment|payout|cover(age)?)|you are (definitely|certainly|fully) covered"
    r"|you will (definitely|certainly) be (paid|covered)|legal advice|you should sue|lawsuit)\b",
    re.IGNORECASE,
)
SHINGLE_WORDS = 12
WORD_RE = re.compile(r"[a-z0-9$€£%]+(?:[.,][0-9]+)?")


def shingles(text: str, n: int = SHINGLE_WORDS) -> set[str]:
    """Hashed n-word shingles of a text (lower-case words). Hashes cannot be turned back into the text."""
    words = WORD_RE.findall((text or "").lower())
    return {hashlib.blake2b(" ".join(words[i:i + n]).encode(), digest_size=8).hexdigest()
            for i in range(len(words) - n + 1)}


async def _llm_check(text: str) -> InjectionCheck | None:
    try:
        return await structured(InjectionCheck, mini=True).ainvoke(prompts.INJECTION.format(text=text[:4000]))
    except Exception as e:  # noqa: BLE001 - never let the classifier break the chat
        log.warning("LLM injection check failed: %s", e)
        return None


async def guard_input(state: ChatState) -> dict:
    text = state.get("message", "")
    for name, pattern in (("prompt injection", INJECTION_RE), ("confidentiality bypass", BYPASS_RE)):
        if m := pattern.search(text):
            info = {"verdict": "BLOCK", "method": "pattern", "kind": name, "excerpt": m.group(0)}
            return {"blocked": True, "guard_in": info,
                    **event("guard_input", f"Blocked: {name} detected", "fail", **info)}

    chk = await _llm_check(text)
    if chk and chk.is_injection and chk.score >= LLM_INJECTION_THRESHOLD:
        info = {"verdict": "BLOCK", "method": "llm", "kind": "manipulation attempt", "score": chk.score}
        return {"blocked": True, "guard_in": info,
                **event("guard_input", f"Blocked: manipulation attempt (score {chk.score:.2f})", "fail", **info)}

    clean, pii = mask_pii(text)
    info = {"verdict": "PASS", "pii_engine": PII_ENGINE, "pii_masked": len(pii), "pii_types": sorted(set(pii))}
    return {"blocked": False, "clean_message": clean, "guard_in": info,
            **event("guard_input", f"No injection · {len(pii)} PII item(s) masked", **info)}


def route_after_input(state: ChatState) -> str:
    return "respond" if state.get("blocked") else "rewrite_query"


async def guard_output(state: ChatState) -> dict:
    answer = dict(state.get("answer") or {})
    text = answer.get("answer", "")
    known = set(state.get("context_clauses", []))
    violations = []

    cited = set(CITATION.findall(text)) | set(answer.get("citations", []))
    unknown = sorted(c for c in cited if c not in known)
    if unknown:
        violations.append(f"Cites clauses that are not in the (public) context: {', '.join(unknown)}")
    if BANNED.search(text):
        violations.append("Contains a promise of coverage/payment or legal advice")
    leaked = shingles(text) & set(state.get("confidential_shingles", []))
    if leaked:
        violations.append(f"Overlaps with confidential text ({len(leaked)} passage(s))")

    masked, pii = mask_pii(text)
    answer["answer"] = masked
    regenerate = bool(violations) and state.get("guard_retries", 0) < 1
    result = {"violations": violations, "pii_masked": len(pii), "regenerate": regenerate}
    if not violations:
        msg, status = f"Output checks passed · {len(pii)} PII item(s) masked", "done"
    else:
        msg = f"{len(violations)} violation(s) → " + ("regenerate answer" if regenerate else "safe fallback")
        status = "warn" if regenerate else "fail"
    update = {"answer": answer, "guard_out": result, **event("guard_output", msg, status, **result)}
    if regenerate:
        update["guard_retries"] = state.get("guard_retries", 0) + 1
        update["feedback"] = "Output check failed: " + "; ".join(violations) + \
            ". Do not promise coverage, only cite clauses from the context, and paraphrase in your own words."
    return update


def route_after_output(state: ChatState) -> str:
    g = state.get("guard_out") or {}
    return "answer_agent" if g.get("violations") and g.get("regenerate") else "respond"
