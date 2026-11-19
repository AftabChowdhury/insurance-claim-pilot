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
