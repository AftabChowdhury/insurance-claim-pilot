"""Claims page: submit claims, watch the agents work, review as an adjuster."""
from datetime import date

import streamlit as st

from common import api

ICON = {"done": "✅", "warn": "⚠️", "fail": "❌", "running": "⏳"}
NODE_LABEL = {
    "guard_input": "Input guardrails", "intake_agent": "Intake agent", "rewrite_query": "Query rewrite",
    "hybrid_search": "Hybrid search", "semantic_rank": "Semantic ranker", "build_context": "Context builder",
    "coverage_agent": "Coverage analyst", "fraud_agent": "Fraud detector", "writer_agent": "Letter writer",
    "evaluator_agent": "Evaluator", "prepare_retry": "Retry", "guard_output": "Output guardrails",
    "human_review": "Adjuster review", "close_claim": "Close claim (MCP)",
}
STATUS_BADGE = {"running": "🔵 Running", "awaiting_review": "🟠 Awaiting adjuster", "completed": "🟢 Completed",
                "failed": "🔴 Failed", "not_started": "⚪ Historic record"}

# ---------------------------------------------------------------------------
# Sidebar: submit a claim
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🛡️ ClaimPilot")
    st.caption("Multi-agent claims copilot")
    st.subheader("Submit a claim")
    loss_types = api("GET", "/loss-types") or {}
    with st.form("new_claim"):
        policy = st.text_input("Policy number", "HO-55821")
        loss_type = st.selectbox("Loss type", list(loss_types), format_func=lambda k: loss_types.get(k, k))
        amount = st.number_input("Claimed amount ($)", min_value=1.0, value=3200.0, step=100.0)
        loss_date = st.date_input("Date of loss", value=date.today(), max_value=date.today())
        description = st.text_area("What happened?", "Pipe under the kitchen sink burst overnight and flooded the floor.")
        files = st.file_uploader("Documents (claim form, invoices, photos, police report)", accept_multiple_files=True,
                                 type=["pdf", "png", "jpg", "jpeg", "txt", "md", "docx"])
        submitted = st.form_submit_button("Submit claim", type="primary", use_container_width=True)
    st.caption("Sample documents: data/sample_claims/")

    if submitted:
        if not files:
            st.error("Attach at least one document.")
        elif len(description.strip()) < 10:
            st.error("Describe what happened (at least 10 characters).")
        else:
            res = api("POST", "/claims",
                      data={"policy_number": policy, "loss_type": loss_type, "amount": amount,
                            "loss_date": loss_date.isoformat(), "description": description},
                      files=[("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files])
            if res:
                st.session_state.claim_id = res["claim_id"]
                st.success(f"Submitted {res['claim_id']}")
                st.rerun()

# ---------------------------------------------------------------------------
# Main: pick a claim
# ---------------------------------------------------------------------------
claims = api("GET", "/claims") or []
if not claims:
    st.info("No claims yet. Submit one from the sidebar.")
    st.stop()

ids = [c["id"] for c in claims]
by_id = {c["id"]: c for c in claims}
current = st.session_state.get("claim_id")
claim_id = st.selectbox(
    "Claim", ids, index=ids.index(current) if current in ids else 0,
    format_func=lambda i: f"{i} · {by_id[i]['policy_number']} · ${by_id[i]['amount']:,.0f} · {by_id[i]['status']}")
st.session_state.claim_id = claim_id

first = api("GET", f"/claims/{claim_id}")
if not first:
    st.stop()


def render(cid: str):
    v = api("GET", f"/claims/{cid}")
    if not v:
        return
    # When the run pauses or finishes, rerun the whole page so the review panel appears / refresh stops.
    if v["status"] != st.session_state.get(f"status_{cid}"):
        changed = f"status_{cid}" in st.session_state
        st.session_state[f"status_{cid}"] = v["status"]
        if changed:
            st.rerun()

    rec, letter, ev, fraud = v.get("record") or {}, v.get("letter") or {}, v.get("evaluation") or {}, v.get("fraud") or {}
    st.markdown(f"### {cid} &nbsp; {STATUS_BADGE.get(v['status'], v['status'])}")
    if v.get("error"):
        st.error(v["error"])

    c = st.columns(6)
    c[0].metric("Claimed", f"${rec.get('amount', 0):,.0f}")
    c[1].metric("Decision", (v.get("final") or {}).get("status") or (letter.get("decision") or "—"))
    payout = (v.get("final") or {}).get("payout", letter.get("payout") or rec.get("payout") or 0)
    c[2].metric("Payout", f"${payout:,.0f}")
    c[3].metric("Eval (faithfulness)", f"{ev['faithfulness']:.2f}" if ev else "—",
                ("pass" if ev.get("passed") else "fail") if ev else None)
    c[4].metric("Fraud risk", f"{fraud['risk_score']:.2f}" if fraud else "—", fraud.get("level"))
    c[5].metric("Retries", v.get("retries", 0))

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
        tabs = st.tabs(["Letter", "Coverage", "Retrieved clauses", "Evaluation", "Guardrails", "Claim record"])
        with tabs[0]:
            if letter:
                st.markdown(f"**Decision:** `{letter.get('decision')}` · **Payout:** ${letter.get('payout', 0):,.0f} · "
                            f"**Citations:** {', '.join(letter.get('citations', [])) or '—'}")
                st.markdown(letter.get("letter", ""))
            else:
                st.caption("No letter yet.")
        with tabs[1]:
            st.json(v.get("coverage") or {}, expanded=True)
            if fraud:
                st.markdown("**Fraud assessment**")
                st.json(fraud, expanded=False)
        with tabs[2]:
            for r in v.get("ranked", []):
                with st.expander(f"[{r['clause_id']}] {r['title']} · reranker {r.get('reranker_score') or 0:.2f}"):
                    st.write(r.get("content"))
        with tabs[3]:
            st.json(ev, expanded=True)
        with tabs[4]:
            st.markdown("**Input**")
            st.json(v.get("guard_in") or {}, expanded=False)
            st.markdown("**Output**")
            st.json(v.get("guard_out") or {}, expanded=False)
        with tabs[5]:
            st.json(rec, expanded=False)


running = first["status"] == "running"
st.fragment(run_every=2 if running else None)(render)(claim_id)

# ---------------------------------------------------------------------------
# Human in the loop
# ---------------------------------------------------------------------------
if first["status"] == "awaiting_review" and first.get("review_request"):
    req = first["review_request"]
    st.divider()
    with st.container(border=True):
        st.markdown("### 🧑‍⚖️ Adjuster review required")
        for r in req.get("reasons", []):
            st.markdown(f"- {r}")
        col1, col2 = st.columns(2)
        decision = col1.selectbox("Decision", ["approve", "deny", "need_info"],
                                  index=["approve", "deny", "need_info"].index(req.get("decision") or "approve"))
        payout = col2.number_input("Payout ($)", min_value=0.0, value=float(req.get("payout") or 0), step=100.0)
        letter = st.text_area("Decision letter", req.get("draft_letter", ""), height=220, key=f"letter_{claim_id}")
        note = st.text_input("Note for the audit log", "")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("✅ Approve & send", type="primary"):
            api("POST", f"/claims/{claim_id}/review", json={"action": "approve", "letter": letter, "decision": decision,
                                                             "payout": payout, "reviewer": "demo.adjuster", "note": note})
            st.rerun()
        if b2.button("⛔ Reject / escalate"):
            api("POST", f"/claims/{claim_id}/review", json={"action": "reject", "reviewer": "demo.adjuster", "note": note})
            st.rerun()

with st.expander("Agent graph (Mermaid)"):
    st.code(api("GET", "/graph") or "", language="text")
    st.caption("Paste into https://mermaid.live to render.")
