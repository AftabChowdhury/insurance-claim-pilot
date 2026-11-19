"""ClaimPilot Streamlit UI: Policy Chat (questions about the policies), Submit Claim and My Claims (customer
login, claim request form, claim status) and Admin (policy documents, claims review with agent timeline and
adjuster review, customers).

Run: uv run streamlit run ui/app.py
"""
import streamlit as st

st.set_page_config(page_title="ClaimPilot", page_icon="🛡️", layout="wide")

st.navigation([
    st.Page("chat_page.py", title="Policy Chat", icon="💬", default=True),
    st.Page("submit_claim_page.py", title="Submit Claim", icon="🧾", url_path="submit-claim"),
    st.Page("my_claims_page.py", title="My Claims", icon="📋", url_path="my-claims"),
    st.Page("admin_page.py", title="Admin", icon="🔐", url_path="admin"),
]).run()
