"""Policy-chat agents (see features/POLICY_CHAT.md §4 for who does what).

Query Agent          rewrite_query       standalone question + 2-4 search queries + policy type
Retrieval Agent      hybrid_search       hybrid (BM25 + vector, RRF) + semantic ranker on PUBLIC chunks only
                     confidential_probe  same search on confidential chunks – keeps scores + hashed shingles only
                     semantic_rank       reranker threshold + top k
                     build_context       [Clause N.M] blocks with parent section context
Access Control Agent access_check        public | partial | restricted | not_found (rules, no LLM)
Answer Agent         answer_agent        grounded answer with clause citations
Evaluator Agent      evaluator_agent     LLM-as-judge + deterministic citation checks -> pass / retry / fallback
Retry Coordinator    prepare_retry       re-retrieve (retrieval problem) or regenerate (generation problem)
Responder            respond             final message, status, citations, history, audit
"""
import asyncio
import json
import re

from api.graph.guardrails import CITATION, mask_pii
from core import db, rag
from core.config import get_settings
from core.llm import structured

from . import prompts
from .guardrails import shingles
from .schemas import ChatJudgement, ChatQuery, PolicyAnswer
from .state import ChatState, event

PROBE_TOP = 3  # confidential chunks per query the probe looks at
BLOCKED_MESSAGE = ("I can't help with that request. I can answer questions about your insurance policies – "
                   "for example what is covered, deductibles, limits or how to make a claim.")
NOT_FOUND_MESSAGE = ("I couldn't find this in the policy documents I can access. Try rephrasing your question, "
                     "or contact your insurance advisor.")
FALLBACK_MESSAGE = "I couldn't find a reliable answer to this in the policy documents I can access."
PARTIAL_NOTE = ("🔒 Some related information is in a confidential document and is not included in this answer. "
                "Please contact your insurance advisor or claims handler for details.")
CLAUSE_PREFIX = re.compile(r"^\s*(clause|§)\s*", re.IGNORECASE)
NOT_IN_DOCS = re.compile(
    r"\b(documents?|polic(y|ies)|wording|context)\b[^.]{0,60}?\b(do(es)?\s+not|don't|doesn't|never)\s+"
    r"(say|specify|mention|state|include|contain|describe|provide|refer)", re.IGNORECASE)


def normalize_citation(c: str) -> str:
    return CLAUSE_PREFIX.sub("", str(c)).strip().strip("[]")


def _history(state: ChatState) -> str:
    turns = get_settings().chat_history_turns
    lines = [f"{m['role']}: {m['content']}" for m in (state.get("history") or [])[-2 * turns:]]
    return "\n".join(lines) or "(no previous messages)"


def _policy_types(state: ChatState, detected: str) -> list[str] | None:
    """UI filter wins, else the detected type; 'general' documents always apply."""
    chosen = state.get("policy_filter") or (detected if detected in ("home", "auto") else None)
    return [chosen, "general"] if chosen else None


# ----------------------------------------------------------------------------------------------- Query Agent
async def rewrite_query(state: ChatState) -> dict:
    retry = state.get("retries", 0)
    retry_block = ""
    if retry:
        retry_block = prompts.RETRY_BLOCK.format(retry=retry, previous=json.dumps(state.get("queries", [])),
                                                 feedback=state.get("feedback", ""))
    out: ChatQuery = await structured(ChatQuery, mini=True).ainvoke(prompts.REWRITE.format(
        history=_history(state), message=state["clean_message"], retry_block=retry_block))
    queries = [q for q in out.queries if q.strip()][:4] or [out.standalone_question]
    types = _policy_types(state, out.policy_type)
    return {"standalone_question": out.standalone_question, "queries": queries, "policy_type": types,
            **event("rewrite_query", f"“{out.standalone_question}” → {len(queries)} sub-queries"
                    + (f" · {'/'.join(types)}" if types else ""),
                    queries=queries, policy_type=types, attempt=retry + 1)}


# ----------------------------------------------------------------------------------------------- Retrieval Agent
async def hybrid_search(state: ChatState) -> dict:
    """Public chunks only: filter `confidential ne true` in Azure AI Search + post-filter in code."""
    queries = state["queries"]
    results = await rag.multi_search(queries, state.get("policy_type"), access="public")
    candidates = rag.merge_hits(queries, results)
    best = rag.best_score(candidates)
    total = sum(len(r) for r in results)
    return {"candidates": candidates, "best_public_score": best,
            **event("hybrid_search", f"{len(queries)} queries → {total} hits → {len(candidates)} unique public chunks "
                    f"(BM25 + vector, RRF, semantic ranker)", hits=total, unique=len(candidates),
                    best_reranker=round(best, 2))}


async def confidential_probe(state: ChatState) -> dict:
    """Is the answer in a confidential document? Only the reranker score and hashed shingles (for the output
    leak check) leave this node – the text is discarded here and never reaches the state, an LLM or the UI."""
    queries = state["queries"]
    results = await rag.multi_search(queries, state.get("policy_type"), access="confidential_only",
                                     top=PROBE_TOP, fields=["id", "doc_id", "content"])
    hits = sorted(rag.merge_hits(queries, results), key=lambda h: h.get("reranker_score") or 0,
                  reverse=True)[:PROBE_TOP]
    score = rag.best_score(hits)
    hashed: set[str] = set()
    for h in hits:
        hashed |= shingles(h.get("content") or "")
    del hits, results
    return {"confidential_score": score, "confidential_shingles": sorted(hashed),
            **event("confidential_probe", "Confidential documents checked (scores only, no content loaded)")}


async def semantic_rank(state: ChatState) -> dict:
    s = get_settings()
    top_k = state.get("top_k") or s.chat_top_k
    min_score = state.get("min_reranker_score") or s.min_reranker_score
    ranked = rag.rank(state.get("candidates", []), top_k, min_score)
    top = [{"clause": c["clause_id"], "title": c["title"], "reranker_score": round(c.get("reranker_score") or 0, 2)}
           for c in ranked]
    msg = ("top: " + ", ".join(f"{t['clause']} ({t['reranker_score']})" for t in top[:3])) if top \
        else f"no public chunk ≥ {min_score}"
    return {"ranked": ranked, **event("semantic_rank", msg, top=top, top_k=top_k, min_score=min_score)}


async def build_context(state: ChatState) -> dict:
    ctx = rag.build_context(state.get("ranked", []), get_settings().context_char_budget)
    sources, seen = [], set()
    for c in state.get("ranked", []):
        key = (c.get("doc_id") or c.get("source"), c["clause_id"])
        if c["clause_id"] in ctx.clauses and key not in seen:
            seen.add(key)
            sources.append({"clause_id": c["clause_id"], "title": c["title"], "section": c.get("section"),
                            "source": c.get("source"), "page": c.get("page"), "policy_type": c.get("policy_type"),
                            "reranker_score": round(c.get("reranker_score") or 0, 2), "content": c["content"]})
    return {"context": ctx.public_text, "context_clauses": ctx.public_clauses, "sources": sources,
            **event("build_context", f"{ctx.blocks} blocks · ~{ctx.chars // 4} tokens · clauses "
                    f"{', '.join(ctx.public_clauses) or '—'}", clauses=ctx.public_clauses, chars=ctx.chars)}


# ----------------------------------------------------------------------------------------------- Access Control
async def access_check(state: ChatState) -> dict:
    s = get_settings()
    public_ok = bool(state.get("ranked"))
    conf = state.get("confidential_score") or 0
    conf_ok = conf >= s.confidential_probe_min_score
    # a confidential chunk far more relevant than anything public: the public hits are only loosely related
    dominant = conf_ok and conf - (state.get("best_public_score") or 0) >= s.confidential_dominance_margin
    if conf_ok and (not public_ok or dominant):
        access, msg, status = "restricted", "Answer only in confidential documents – withheld", "warn"
    elif conf_ok:
        access, msg, status = "partial", "Answer from public clauses; related confidential information withheld", "warn"
    elif public_ok:
        access, msg, status = "public", "Public policy clauses only", "done"
    else:
        access, msg, status = "not_found", "No relevant policy clauses found", "warn"
    return {"access": access, **event("access_check", msg, status, access=access)}


def route_after_access(state: ChatState) -> str:
    return "answer_agent" if state.get("access") in ("public", "partial") else "respond"


# ----------------------------------------------------------------------------------------------- Answer Agent
async def answer_agent(state: ChatState) -> dict:
    feedback = state.get("feedback", "")
    regenerate = state.get("retry_target") == "generation" or state.get("guard_retries", 0) > 0
    out: PolicyAnswer = await structured(PolicyAnswer).ainvoke(prompts.ANSWER.format(
        history=_history(state), question=state["standalone_question"], context=state["context"],
        allowed=", ".join(state.get("context_clauses", [])) or "(none)",
        feedback_block=prompts.FEEDBACK_BLOCK.format(feedback=feedback) if regenerate and feedback else ""))
    answer = out.model_dump()
    answer["citations"] = list(dict.fromkeys(normalize_citation(c) for c in answer["citations"]))
    if answer["answer_coverage"] == "full" and NOT_IN_DOCS.search(answer["answer"]):
        answer["answer_coverage"] = "partial"  # the answer itself says a detail is missing
    attempts = state.get("attempts", 0) + 1
    return {"answer": answer, "attempts": attempts,
            **event("answer_agent", f"Attempt {attempts}: {len(answer['answer'].split())} words · citations "
                    f"{', '.join(answer['citations']) or '—'} · public context answers it: "
                    f"{answer['answer_coverage']}", attempt=attempts, confidence=answer["confidence"],
                    answer_coverage=answer["answer_coverage"])}


def route_after_answer(state: ChatState) -> str:
    """The public clauses do not answer it, but a confidential document does -> restricted, nothing to evaluate."""
    if state.get("access") == "partial" and (state.get("answer") or {}).get("answer_coverage") == "none":
        return "respond"
    return "evaluator_agent"


# ----------------------------------------------------------------------------------------------- Evaluator Agent
async def evaluator_agent(state: ChatState) -> dict:
    s = get_settings()
    answer = state["answer"]
    out: ChatJudgement = await structured(ChatJudgement, mini=True).ainvoke(prompts.JUDGE.format(
        question=state["standalone_question"], context=state["context"], answer=answer["answer"]))
    j = out.model_dump()

    # deterministic checks on top of the judge
    known = set(state.get("context_clauses", []))
    cited = {normalize_citation(c) for c in CITATION.findall(answer["answer"])} | set(answer.get("citations", []))
    invalid = sorted(c for c in cited if c not in known)
    j["invalid_citations"] = invalid
    if invalid:
        j["citation_coverage"] = min(j["citation_coverage"], 0.5)
        j["unsupported_statements"].append(f"Cited clauses not in the context: {', '.join(invalid)}")

    passed = (j["faithfulness"] >= s.chat_eval_threshold and j["answer_relevance"] >= s.chat_min_relevance
              and j["citation_coverage"] >= s.chat_min_citation_coverage and not invalid)
    if passed:
        j["failure_cause"] = "none"
    elif j["failure_cause"] == "none":
        j["failure_cause"] = "generation"
    j.update(passed=passed, attempt=state.get("attempts", 1),
             thresholds={"faithfulness": s.chat_eval_threshold, "answer_relevance": s.chat_min_relevance,
                         "citation_coverage": s.chat_min_citation_coverage})
    msg = (f"faithfulness {j['faithfulness']:.2f} · relevance {j['answer_relevance']:.2f} · citations "
           f"{j['citation_coverage']:.2f} → {'PASS' if passed else 'FAIL (' + j['failure_cause'] + ')'}")
    return {"evaluation": j, **event("evaluator_agent", msg, "done" if passed else "fail", **j)}


def route_after_eval(state: ChatState) -> str:
    if state["evaluation"].get("passed"):
        return "guard_output"
    if state.get("retries", 0) < get_settings().chat_max_retries:
        return "prepare_retry"
    return "respond"  # out of retries -> safe fallback, the failed answer is not shown


# ----------------------------------------------------------------------------------------------- Retry Coordinator
async def prepare_retry(state: ChatState) -> dict:
    s = get_settings()
    retries = state.get("retries", 0) + 1
    ev = state["evaluation"]
    feedback = ev.get("feedback", "")
    if ev.get("unsupported_statements"):
        feedback += " Unsupported: " + "; ".join(ev["unsupported_statements"])
    if ev.get("missing_information"):
        feedback += " Missing: " + "; ".join(ev["missing_information"])
    if ev.get("invalid_citations"):
        feedback += (f" Do NOT cite {', '.join(ev['invalid_citations'])} – those clauses are not in the context. "
                     f"Only cite: {', '.join(state.get('context_clauses', []))}.")
    update: dict = {"retries": retries, "feedback": feedback.strip()}
    if ev.get("failure_cause") == "retrieval":
        top_k = (state.get("top_k") or s.chat_top_k) + 3
        min_score = max(0.5, (state.get("min_reranker_score") or s.min_reranker_score) - 0.5)
        update.update(retry_target="retrieval", top_k=top_k, min_reranker_score=min_score)
        msg = f"Retry {retries}/{s.chat_max_retries}: re-retrieve (top_k → {top_k}, min score → {min_score})"
    else:
        update["retry_target"] = "generation"
        msg = f"Retry {retries}/{s.chat_max_retries}: regenerate the answer with the evaluator's feedback"
    return {**update, **event("prepare_retry", msg, "warn", feedback=update["feedback"])}


def route_after_retry(state: ChatState) -> str:
    return "rewrite_query" if state.get("retry_target") == "retrieval" else "answer_agent"


# ----------------------------------------------------------------------------------------------- Responder
def _audit(state: ChatState, action: str) -> None:
    question = state.get("clean_message") or mask_pii(state.get("message", ""))[0]
    db.add_audit(db.CHAT_AUDIT_ID, f"chat:{state.get('session_id', '')[:8]}", action, question[:300])


async def respond(state: ChatState) -> dict:
    s = get_settings()
    access = state.get("access")
    evaluation = state.get("evaluation") or {}
    guard = state.get("guard_out") or {}
    answer = state.get("answer") or {}
    citations, suggestions, audit = [], [], None

    coverage = answer.get("answer_coverage")
    if state.get("blocked"):
        status, text, audit = "blocked", BLOCKED_MESSAGE, "chat_blocked"
    elif access == "restricted" or (access == "partial" and coverage == "none"):
        status, text, audit = "restricted", s.confidential_message, "chat_restricted"
    elif access == "not_found":
        status, text = "not_found", NOT_FOUND_MESSAGE
    elif not evaluation.get("passed") or guard.get("violations"):
        if access == "partial":  # public clauses could not answer it; the rest is confidential
            status, text, audit = "restricted", s.confidential_message, "chat_restricted"
        else:
            status = "fallback"
            top = state.get("sources", [])[:3]
            text = FALLBACK_MESSAGE
            if top:
                text += " These clauses look related – please read them or contact your insurance advisor:\n" + \
                    "\n".join(f"- {c['title']} ({c['source']})" for c in top)
                citations = top
    else:
        # the confidential note only when the public clauses answer the question just partly
        status = "partial" if access == "partial" and coverage != "full" else "answered"
        text = answer.get("answer", "")
        if status == "partial":
            text += "\n\n" + PARTIAL_NOTE
        cited = set(answer.get("citations", [])) | {normalize_citation(c) for c in CITATION.findall(text)}
        citations = [c for c in state.get("sources", []) if c["clause_id"] in cited]
        suggestions = answer.get("follow_up_suggestions", [])[:3]

    if audit:
        await asyncio.to_thread(_audit, state, audit)
    result = {
        "answer": text, "status": status,
        "citations": [{k: c.get(k) for k in ("clause_id", "title", "section", "source", "page", "policy_type",
                                             "reranker_score", "content")} for c in citations],
        "suggestions": suggestions,
        "standalone_question": state.get("standalone_question"),
        "evaluation": {k: evaluation.get(k) for k in ("faithfulness", "answer_relevance", "citation_coverage",
                                                     "passed", "failure_cause")} if evaluation else None,
        "attempts": state.get("attempts", 0),
        "retries": state.get("retries", 0),
    }
    user_turn = "[blocked message]" if state.get("blocked") else state.get("clean_message", "")
    history = [{"role": "user", "content": user_turn},
               {"role": "assistant", "content": text, "status": status}]
    return {"result": result, "history": history, **event("respond", f"Status: {status}",
                                                          "done" if status in ("answered", "partial") else "warn")}
