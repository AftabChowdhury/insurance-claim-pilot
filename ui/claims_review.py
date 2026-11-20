"""Claims review (Admin panel tab): claims waiting for a decision first, the agent timeline, image evidence and the
admin decision (Accept / Reject with reason; security-flagged claims: Reject or Release for the claim check)."""
import streamlit as st

from common import admin_api, admin_file, eur

REVIEW_STATES = ("AWAITING_REVIEW", "FLAGGED_SECURITY")
ICON = {"done": "✅", "warn": "⚠️", "fail": "❌", "running": "⏳"}
NODE_LABEL = {
    "verify_evidence": "Evidence check", "guard_input": "Input guardrails", "intake_agent": "Intake agent",
    "rewrite_query": "Query rewrite", "hybrid_search": "Hybrid search", "semantic_rank": "Semantic ranker",
    "build_context": "Context builder", "coverage_agent": "Coverage analyst", "fraud_agent": "Fraud detector",
    "writer_agent": "Letter writer", "evaluator_agent": "Evaluator", "prepare_retry": "Retry",
    "guard_output": "Output guardrails", "human_review": "Admin review", "close_claim": "Close claim (MCP)",
}
STATUS_BADGE = {"running": "🔵 Checking", "awaiting_review": "🟠 Awaiting admin", "completed": "🟢 Completed",
                "failed": "🔴 Failed", "not_started": "⚪ No check run"}
CLAIM_BADGE = {"AWAITING_REVIEW": "🟠", "FLAGGED_SECURITY": "🛑", "CHECKING": "🔵", "RECEIVED": "⚪",
               "APPROVED": "🟢", "DENIED": "🔴", "PENDING_INFO": "🟣", "ESCALATED": "🟠"}
VERDICT = {"verified": ":green-badge[verified]", "suspicious": ":red-badge[suspicious]",
           "unusable": ":gray-badge[unusable]", "unverified": ":orange-badge[not analysed]"}


def _evidence_tab(cid: str, v: dict) -> None:
    summary = v.get("evidence_summary") or {}
    evidence = v.get("evidence") or []
    if not evidence:
        st.caption("No images or scanned documents in this claim.")
        return
    st.caption(f"{summary.get('verified', 0)} verified · {summary.get('suspicious', 0)} suspicious · "
               f"{summary.get('unusable', 0)} unusable · {summary.get('unverified', 0)} not analysed"
               + (f" · documented invoice total {eur(summary['documented_total'])}"
                  if summary.get("documented_total") else ""))
    for e in evidence:
        with st.container(border=True):
            st.markdown(f"**{e['name']}** {VERDICT.get(e['verdict'], e['verdict'])}")
            left, right = st.columns([2, 3])
            if e["kind"] == "image" and (data := admin_file(f"/claims/{cid}/files/{e['name']}")):
                left.image(data, width="stretch")
            vis = e.get("vision") or {}
            with right:
                if vis:
                    st.markdown(f"`{vis['image_type']}` · damage `{vis['damage_type']}` · matches loss "
                                f"`{vis['matches_loss_type']}` · quality `{vis['quality']}`")
                    st.write(vis["description"])
                    if vis.get("total_amount"):
                        st.markdown(f"Invoice: {vis.get('vendor') or '?'} · {vis.get('document_date') or '?'} · "
                                    f"{vis['total_amount']:,.2f} {vis.get('currency') or ''}")
                for sig in e.get("signals", []):
                    st.markdown(f"{'🚩' if sig['strong'] else '⚠️'} {sig['message']}")
                    if sig.get("excerpt"):
                        st.code(sig["excerpt"], language="text")
                exif = e.get("exif") or {}
                if exif:
                    st.caption(f"EXIF: taken {exif.get('date_taken') or '—'} · {exif.get('make') or ''} "
                               f"{exif.get('model') or ''} · software {exif.get('software') or '—'} · "
                               f"GPS {'yes' if exif.get('gps') else 'no'}")
            if vis.get("visible_text"):
                with st.expander("Text in the image"):
                    st.text(vis["visible_text"])


def render(cid: str):
    v = admin_api("GET", f"/claims/{cid}")
    if not v:
        return
    # When the run pauses or finishes, rerun the whole page so the review panel appears / refresh stops.
    if v["status"] != st.session_state.get(f"status_{cid}"):
        changed = f"status_{cid}" in st.session_state
        st.session_state[f"status_{cid}"] = v["status"]
        if changed:
            st.rerun()

    rec, letter, ev, fraud = v.get("record") or {}, v.get("letter") or {}, v.get("evaluation") or {}, v.get("fraud") or {}
    final = v.get("final") or {}
    st.markdown(f"### {cid} &nbsp; {STATUS_BADGE.get(v['status'], v['status'])} &nbsp; :gray[{rec.get('status')}]")
    st.caption(f"Customer {rec.get('customer_id') or '—'} · policy {rec.get('policy_number')} · "
               f"loss date {rec.get('loss_date') or '—'}")
    if v.get("error"):
        st.error(v["error"])

    c = st.columns(6)
    c[0].metric("Claimed", eur(rec.get("amount", 0)))
    c[1].metric("Decision", final.get("status") or (letter.get("decision") or "—"))
    payout = final.get("payout", letter.get("payout") or rec.get("payout") or 0)
    c[2].metric("Payout", eur(payout))
    c[3].metric("Eval (faithfulness)", f"{ev['faithfulness']:.2f}" if ev else "—",
                ("pass" if ev.get("passed") else "fail") if ev else None)
    c[4].metric("Fraud risk", f"{fraud['risk_score']:.2f}" if fraud else "—", fraud.get("level"))
    c[5].metric("Evidence", f"{(v.get('evidence_summary') or {}).get('suspicious', 0)} suspicious"
                if v.get("evidence") else "—")
    if reason := (final.get("reason") or rec.get("decision_reason")):
        st.info(f"**Reason given to the customer:** {reason}")

    left, right = st.columns([5, 7])
    with left:
        st.markdown("#### Agent timeline")
        if not v["events"]:
            st.caption("No workflow run for this claim (historic record).")
        for e in v["events"]:
            st.markdown(f"{ICON.get(e['status'], '•')} **{NODE_LABEL.get(e['node'], e['node'])}** — {e['message']}")
        if v["status"] == "running":
            st.markdown("⏳ _working…_")
    with right:
        tabs = st.tabs(["Letter", "Evidence", "Coverage", "Retrieved clauses", "Evaluation", "Guardrails", "Record"])
        with tabs[0]:
            if letter:
                st.markdown(f"**Decision:** `{letter.get('decision')}` · **Payout:** {eur(letter.get('payout', 0))} · "
                            f"**Citations:** {', '.join(letter.get('citations', [])) or '—'}")
                if letter.get("reason"):
                    st.markdown(f"**Reason:** {letter['reason']}")
                st.markdown(letter.get("letter", ""))
            else:
                st.caption("No letter yet.")
        with tabs[1]:
            _evidence_tab(cid, v)
        with tabs[2]:
            st.json(v.get("coverage") or {}, expanded=True)
            if fraud:
                st.markdown("**Fraud assessment**")
                st.json(fraud, expanded=False)
        with tabs[3]:
            for r in v.get("ranked", []):
                with st.expander(f"[{r['clause_id']}] {r['title']} · reranker {r.get('reranker_score') or 0:.2f}"):
                    st.write(r.get("content"))
        with tabs[4]:
            st.json(ev, expanded=True)
        with tabs[5]:
            st.markdown("**Input**")
            st.json(v.get("guard_in") or {}, expanded=False)
            st.markdown("**Output**")
            st.json(v.get("guard_out") or {}, expanded=False)
        with tabs[6]:
            st.json(rec, expanded=False)


def _review_panel(claim_id: str, rec: dict, req: dict) -> None:
    security = req.get("kind") == "security"
    st.divider()
    with st.container(border=True):
        if security:
            st.markdown("### 🛑 Security review – possible prompt injection")
            guard = req.get("guard") or {}
            st.markdown(f"Flagged source: **{guard.get('source')}** (method `{guard.get('method')}`)")
            st.code(guard.get("excerpt") or "", language="text")
            st.caption("No agent has processed this claim. **Release** runs the normal claim check with the flagged "
                       "content withheld; the claim then comes back here for your final decision.")
        else:
            st.markdown("### 🧑‍⚖️ Admin review required")
            for r in req.get("reasons", []):
                st.markdown(f"- {r}")
            st.caption(f"Proposed by the agents: `{req.get('decision')}` · {eur(req.get('payout') or 0)}"
                       + (f" · {req['reason']}" if req.get("reason") else ""))

        note = st.text_input("Internal note (audit log only)", key=f"note_{claim_id}")
        reason = st.text_area("Reason shown to the customer (required to reject)", key=f"reason_{claim_id}",
                              height=80)
        if security:
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("🔓 Release for check", key=f"release_{claim_id}"):
                if admin_api("POST", f"/claims/{claim_id}/review", json={"action": "release", "note": note}):
                    st.rerun()
        else:
            amount = float(rec.get("amount") or 0)
            default = min(float(req.get("payout") or 0), amount) or amount
            payout = st.number_input("Payout (€)", min_value=0.0, max_value=amount, value=default, step=50.0,
                                     key=f"payout_{claim_id}")
            draft = req.get("draft_letter", "") if req.get("decision") == "approve" else ""
            letter = st.text_area("Decision letter for an acceptance (leave empty to generate one from the payout)",
                                  draft, height=220, key=f"letter_{claim_id}")
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("✅ Accept", type="primary", key=f"accept_{claim_id}"):
                if admin_api("POST", f"/claims/{claim_id}/review",
                             json={"action": "accept", "payout": payout, "letter": letter,
                                   "reason": reason, "note": note}):
                    st.rerun()
        if b2.button("⛔ Reject", key=f"reject_{claim_id}"):
            if len(reason.strip()) < 5:
                st.error("Enter the reason for the customer (at least 5 characters).")
            elif admin_api("POST", f"/claims/{claim_id}/review", json={"action": "reject", "reason": reason,
                                                                         "note": note}):
                st.rerun()


def claims_review() -> None:
    """Render the claims review (call inside the Admin panel tab)."""
    claims = admin_api("GET", "/claims?limit=200") or []
    if not claims:
        st.info("No claims yet.")
        return

    waiting = [c for c in claims if c["status"] in REVIEW_STATES]
    only_waiting = st.toggle(f"Only claims waiting for a decision ({len(waiting)})", value=bool(waiting))
    shown = waiting if only_waiting else sorted(claims, key=lambda c: c["status"] not in REVIEW_STATES)
    if not shown:
        st.success("No claims are waiting for a decision.")
        return

    ids = [c["id"] for c in shown]
    by_id = {c["id"]: c for c in shown}
    current = st.session_state.get("claim_id")
    claim_id = st.selectbox(
        "Claim", ids, index=ids.index(current) if current in ids else 0,
        format_func=lambda i: f"{CLAIM_BADGE.get(by_id[i]['status'], '•')} {i} · {by_id[i].get('customer_id') or '—'}"
                              f" · {eur(by_id[i]['amount'])} · {by_id[i]['status']}")
    st.session_state.claim_id = claim_id

    first = admin_api("GET", f"/claims/{claim_id}")
    if not first:
        return

    running = first["status"] == "running"
    st.fragment(run_every=2 if running else None)(render)(claim_id)

    if first["status"] == "awaiting_review" and first.get("review_request"):
        _review_panel(claim_id, first.get("record") or {}, first["review_request"])

    with st.expander("Agent graph (Mermaid)"):
        st.code(admin_api("GET", "/graph") or "", language="text")
        st.caption("Paste into https://mermaid.live to render.")
