"""Shared helpers for the Streamlit pages."""
import os

import requests
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")


def api(method: str, path: str, *, quiet_status: tuple[int, ...] = (), **kw):
    """Call the ClaimPilot API. Shows errors in the page and returns None on failure.

    Status codes in `quiet_status` are not shown; the response object is returned instead so the caller
    can handle them (e.g. 401 on the admin page).
    """
    try:
        kw.setdefault("timeout", 60)
        r = requests.request(method, API + path, **kw)
    except requests.ConnectionError:
        st.error(f"Cannot reach the API at {API}. Start it with: uv run uvicorn api.main:app --port 8000")
        st.stop()
    if r.status_code in quiet_status:
        return r
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        st.error(f"API error {r.status_code}: {detail}")
        return None
    return r.json() if "json" in r.headers.get("content-type", "") else r.text


# ---------------------------------------------------------------------------
# Customer session, shared by the Submit Claim and My Claims pages
# ---------------------------------------------------------------------------
DEMO_EMAILS = ["emma.schneider@example.com", "lukas.weber@example.com", "sophia.fischer@example.com"]


def customer_logout(message: str | None = None) -> None:
    for k in ("customer_token", "customer"):
        st.session_state.pop(k, None)
    if message:
        st.session_state.customer_login_message = message
    st.rerun()


def customer_api(method: str, path: str, **kw):
    """API call with the customer token; an expired/invalid token sends the customer back to the login form."""
    headers = {"Authorization": f"Bearer {st.session_state.customer_token}"}
    r = api(method, path, headers=headers, quiet_status=(401,), **kw)
    if hasattr(r, "status_code"):  # 401
        customer_logout("Your session has expired. Please log in again.")
    return r


def customer_login() -> dict:
    """Show the customer login form and stop the page until logged in; then show the account bar.

    Returns the logged-in customer {id, name, email, phone}.
    """
    if not st.session_state.get("customer_token"):
        if msg := st.session_state.pop("customer_login_message", None):
            st.warning(msg)
        with st.form("customer_login"):
            st.subheader("Log in")
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary"):
                r = api("POST", "/customer/login", data={"email": email, "password": password},
                        quiet_status=(401,))
                if hasattr(r, "status_code"):
                    st.error("Invalid email or password.")
                elif r:
                    st.session_state.customer_token = r["token"]
                    st.session_state.customer = r["customer"]
                    st.rerun()
        st.caption("Demo: log in with a customer email and the password `user`, e.g. " +
                   ", ".join(f"`{e}`" for e in DEMO_EMAILS))
        st.stop()

    me = st.session_state.customer
    left, right = st.columns([6, 1])
    left.markdown(f"Welcome, **{me['name']}** :gray[({me['id']} · {me['email']})]")
    if right.button("Log out", width="stretch"):
        customer_logout()
    return me
