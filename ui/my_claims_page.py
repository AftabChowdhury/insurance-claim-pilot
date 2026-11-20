"""My Claims: a logged-in customer's claim history with status and, once decided, the reason and decision letter."""
import streamlit as st

from common import customer_api, customer_login, eur

BADGE = {
    "CHECKING": ":blue-badge[Being checked]", "IN_REVIEW": ":orange-badge[In review]",
    "APPROVED": ":green-badge[Approved]", "DENIED": ":red-badge[Declined]",
    "PENDING_INFO": ":violet-badge[Information needed]",
}
OPEN = ("CHECKING",)  # refresh automatically while the claim check runs


def ts(value: str | None) -> str:
    return value.replace("T", " ").split("+")[0] if value else "—"


st.title("📋 My Claims")
customer_login()


def history() -> list[dict]:
    claims = customer_api("GET", "/customer/claims") or []
    st.subheader("Claim history")
    if not claims:
        st.info("You have not submitted any claims yet.")
        st.page_link("submit_claim_page.py", label="Submit a claim", icon="🧾")
        return []
    st.dataframe(
        [{"Claim ID": c["id"], "Loss type": c["loss_type_label"], "Date of loss": c.get("loss_date") or "—",
          "Claimed": eur(c["amount"]), "Status": c["status_label"], "Payout": eur(c["payout"]),
          "Submitted (UTC)": ts(c["created_at"]), "Last update (UTC)": ts(c["updated_at"])} for c in claims],
        hide_index=True, width="stretch")
    return claims


def details(claim_id: str) -> None:
    c = customer_api("GET", f"/customer/claims/{claim_id}")
    if not c:
        return
    if c["status"] != st.session_state.get(f"my_status_{claim_id}"):
        changed = f"my_status_{claim_id}" in st.session_state
        st.session_state[f"my_status_{claim_id}"] = c["status"]
        if changed:
            st.rerun()  # status changed: refresh the history table and stop auto-refresh

    with st.container(border=True):
        st.markdown(f"### {c['id']} &nbsp; {BADGE.get(c['status'], c['status'])}")
        st.caption(c["status_label"])
        m = st.columns(4)
        m[0].metric("Claimed", eur(c["amount"]))
        m[1].metric("Payout", eur(c["payout"]))
        m[2].metric("Loss type", c["loss_type_label"])
        m[3].metric("Policy", c["policy_number"])

        if c["status"] == "CHECKING":
            st.info("⏳ ClaimPilot is checking your claim against your policy. This page updates automatically.")
        elif c["status"] == "IN_REVIEW":
            st.info("A claims adjuster is reviewing your claim. You will see the decision here.")
        elif c["status"] == "DENIED":
            st.error(f"**Reason for the decision:** {c.get('reason') or 'See the decision letter below.'}")
        elif c["status"] == "PENDING_INFO":
            st.warning(f"**We need more information:** {c.get('reason') or 'See the letter below.'}")
        elif c["status"] == "APPROVED":
            st.success(f"Your claim was approved. Payout: **{eur(c['payout'])}**"
                       + (f" – {c['reason']}" if c.get("reason") else ""))

        st.markdown(f"**Date of loss:** {c.get('loss_date') or '—'}  \n**Description**  \n{c['description'] or '—'}")
        st.markdown("**Files**  \n" + (", ".join(c["documents"]) or "—"))
        st.caption(f"Submitted {ts(c['created_at'])} UTC · last update {ts(c['updated_at'])} UTC")
        if c.get("letter"):
            with st.expander("Decision letter", expanded=c["status"] != "APPROVED"):
                st.markdown(c["letter"])


claims = history()
if claims:
    ids = [c["id"] for c in claims]
    current = st.session_state.get("my_claim_id")
    claim_id = st.selectbox("Claim details", ids, index=ids.index(current) if current in ids else 0)
    st.session_state.my_claim_id = claim_id
    status = next(c["status"] for c in claims if c["id"] == claim_id)
    st.fragment(run_every=3 if status in OPEN else None)(details)(claim_id)
