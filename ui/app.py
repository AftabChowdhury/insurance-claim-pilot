"""ClaimPilot Streamlit UI: Policy Chat (questions about the policies), Claims (submit, agent timeline, adjuster
review) and Admin (policy documents).

Run: uv run streamlit run ui/app.py
"""
import streamlit as st

st.set_page_config(page_title="ClaimPilot", page_icon="🛡️", layout="wide")

st.navigation([
    st.Page("chat_page.py", title="Policy Chat", icon="💬", default=True),
    st.Page("claims_page.py", title="Claims", icon="📄", url_path="claims"),
    st.Page("admin_page.py", title="Admin", icon="🔐", url_path="admin"),
]).run()
