"""My Claims: a logged-in customer sees the status of their claims and, once decided, the decision letter."""
import streamlit as st

from common import customer_api, customer_login

BADGE = {
    "RECEIVED": ":blue-badge[Received]", "IN_REVIEW": ":orange-badge[In review]",
    "APPROVED": ":green-badge[Approved]", "DENIED": ":red-badge[Declined]",
    "PENDING_INFO": ":violet-badge[Information needed]",
}


def ts(value: str | None) -> str:
    return value.replace("T", " ").split("+")[0] if value else "—"


st.title("📋 My Claims")
customer_login()

claims = customer_api("GET", "/customer/claims") or []
if not claims:
    st.info("You have not submitted any claims yet.")
    st.page_link("submit_claim_page.py", label="Submit a claim", icon="🧾")
    st.stop()

st.dataframe(
    [{"Claim ID": c["id"], "Loss type": c["loss_type_label"], "Claimed ($)": c["amount"],
      "Status": c["status_label"], "Payout ($)": c["payout"], "Submitted (UTC)": ts(c["created_at"]),
      "Last update (UTC)": ts(c["updated_at"])} for c in claims],
    hide_index=True, width="stretch")

ids = [c["id"] for c in claims]
current = st.session_state.get("my_claim_id")
claim_id = st.selectbox("Claim details", ids, index=ids.index(current) if current in ids else 0)
st.session_state.my_claim_id = claim_id

c = customer_api("GET", f"/customer/claims/{claim_id}")
if not c:
    st.stop()

with st.container(border=True):
    st.markdown(f"### {c['id']} &nbsp; {BADGE.get(c['status'], c['status'])}")
    st.caption(c["status_label"])
    m = st.columns(4)
    m[0].metric("Claimed", f"${c['amount']:,.0f}")
    m[1].metric("Payout", f"${c['payout']:,.0f}" if c["payout"] is not None else "—")
    m[2].metric("Loss type", c["loss_type_label"])
    m[3].metric("Policy", c["policy_number"])
    st.markdown(f"**Description**  \n{c['description'] or '—'}")
    st.markdown("**Files**  \n" + (", ".join(c["documents"]) or "—"))
    st.caption(f"Submitted {ts(c['created_at'])} UTC · last update {ts(c['updated_at'])} UTC")
    if c.get("letter"):
        with st.expander("Decision letter", expanded=True):
            st.markdown(c["letter"])
    elif c["status"] in ("RECEIVED", "IN_REVIEW"):
        st.info("We are reviewing your claim. You will see the decision here.")
