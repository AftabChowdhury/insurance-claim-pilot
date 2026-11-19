"""Submit Claim: a logged-in customer submits a claim request with supporting files.

The claim is stored as RECEIVED; the claim check (multi-agent workflow) is not run yet. Status: My Claims page.
"""
import streamlit as st

from common import api, customer_api, customer_login

FILE_TYPES = ["pdf", "png", "jpg", "jpeg", "txt", "md", "docx"]

st.title("🧾 Submit a Claim")
me = customer_login()

if "claim_uploader_key" not in st.session_state:
    st.session_state.claim_uploader_key = 0
loss_types = api("GET", "/loss-types") or {}

with st.form("customer_claim"):
    st.text_input("Customer ID", value=me["id"], disabled=True)
    loss_type = st.selectbox("Loss type", list(loss_types), format_func=lambda k: loss_types.get(k, k))
    amount = st.number_input("Claimed amount ($)", min_value=1.0, value=1000.0, step=100.0)
    description = st.text_area("Description", placeholder="What happened, when and where?")
    files = st.file_uploader("Upload relevant files (claim form, invoices, photos, police report)",
                             type=FILE_TYPES, accept_multiple_files=True,
                             key=f"claim_files_{st.session_state.claim_uploader_key}")
    submitted = st.form_submit_button("Submit claim", type="primary")

if submitted:
    if not files:
        st.error("Attach at least one file.")
    elif len(description.strip()) < 10:
        st.error("Describe what happened (at least 10 characters).")
    else:
        res = customer_api(
            "POST", "/customer/claims",
            data={"customer_id": me["id"], "loss_type": loss_type, "amount": amount, "description": description},
            files=[("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files])
        if res:
            st.session_state.claim_submitted = res["claim_id"]
            st.session_state.my_claim_id = res["claim_id"]  # preselected on My Claims
            st.session_state.claim_uploader_key += 1  # clear the uploader
            st.rerun()

if cid := st.session_state.pop("claim_submitted", None):
    st.success(f"Claim **{cid}** received. We'll review it and get back to you.")
    st.page_link("my_claims_page.py", label="Track it in My Claims", icon="📋")
