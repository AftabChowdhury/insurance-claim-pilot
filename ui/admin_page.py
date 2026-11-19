"""Admin Panel: static login, upload policy documents (.pdf/.doc/.docx), list and delete them."""
import streamlit as st

from common import api

ALLOWED_TYPES = ["pdf", "doc", "docx"]


def logout(message: str | None = None) -> None:
    for k in ("admin_token", "admin_user", "confirm_delete"):
        st.session_state.pop(k, None)
    if message:
        st.session_state.login_message = message
    st.rerun()


def admin_api(method: str, path: str, **kw):
    """API call with the admin token; an expired/invalid token sends the user back to the login form."""
    headers = {"Authorization": f"Bearer {st.session_state.admin_token}"}
    r = api(method, path, headers=headers, quiet_status=(401,), **kw)
    if hasattr(r, "status_code"):  # 401
        logout("Your session has expired. Please log in again.")
    return r


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


st.title("🔐 Admin Panel")

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
if not st.session_state.get("admin_token"):
    if msg := st.session_state.pop("login_message", None):
        st.warning(msg)
    with st.form("admin_login"):
        st.subheader("Log in")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary"):
            r = api("POST", "/admin/login", data={"username": username, "password": password}, quiet_status=(401,))
            if hasattr(r, "status_code"):
                st.error("Invalid username or password.")
            elif r:
                st.session_state.admin_token = r["token"]
                st.session_state.admin_user = r["username"]
                st.rerun()
    st.stop()

top_l, top_r = st.columns([6, 1])
top_l.caption(f"Logged in as **{st.session_state.get('admin_user', 'admin')}**")
if top_r.button("Log out", use_container_width=True):
    logout()

# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
st.subheader("Upload policy documents")
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
files = st.file_uploader("PDF or Word (.doc, .docx) – you can select several files", type=ALLOWED_TYPES,
                         accept_multiple_files=True, key=f"uploader_{st.session_state.uploader_key}")
if st.button("Upload", type="primary", disabled=not files):
    res = admin_api("POST", "/admin/policies",
                    files=[("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files])
    if res:
        st.session_state.upload_results = res
        st.session_state.uploader_key += 1  # clear the uploader
        st.rerun()

for r in st.session_state.pop("upload_results", []):
    if r["status"] == "uploaded":
        st.success(f"Uploaded **{r['filename']}**")
    else:
        st.error(f"Rejected **{r['filename']}**: {r['error']}")

st.divider()

# ---------------------------------------------------------------------------
# Documents list
# ---------------------------------------------------------------------------
head_l, head_r = st.columns([6, 1])
head_l.subheader("Uploaded documents")
if head_r.button("Refresh", use_container_width=True):
    st.rerun()

docs = admin_api("GET", "/admin/policies") or []
if not docs:
    st.info("No policy documents uploaded yet.")
    st.stop()

by_id = {d["id"]: d for d in docs}
confirm = st.session_state.get("confirm_delete")  # list of doc ids awaiting confirmation

if confirm:
    names = ", ".join(by_id[i]["filename"] for i in confirm if i in by_id)
    with st.container(border=True):
        st.warning(f"Delete {len(confirm)} document(s)? {names}")
        c1, c2, _ = st.columns([1, 1, 5])
        if c1.button("Yes, delete", type="primary"):
            deleted = 0
            for doc_id in confirm:
                if admin_api("DELETE", f"/admin/policies/{doc_id}"):
                    deleted += 1
            st.session_state.pop("confirm_delete", None)
            st.toast(f"Deleted {deleted} document(s)")
            st.rerun()
        if c2.button("Cancel"):
            st.session_state.pop("confirm_delete", None)
            st.rerun()

cols = [4, 1, 1, 2, 3, 1]
hdr = st.columns(cols)
for c, label in zip(hdr, ["File", "Type", "Size", "Uploaded by", "Uploaded at (UTC)", ""]):
    if label:
        c.markdown(f"**{label}**")
for d in docs:
    row = st.columns(cols, vertical_alignment="center")
    row[0].write(d["filename"])
    row[1].write(d["file_type"].upper())
    row[2].write(human_size(d["size_bytes"]))
    row[3].write(d["uploaded_by"])
    row[4].write(d["uploaded_at"].replace("T", " ").split("+")[0])
    if row[5].button("🗑", key=f"del_{d['id']}", help=f"Delete {d['filename']}"):
        st.session_state.confirm_delete = [d["id"]]
        st.rerun()

st.markdown("")
selected = st.multiselect("Select documents to delete", list(by_id), format_func=lambda i: by_id[i]["filename"])
if st.button("Delete selected", disabled=not selected):
    st.session_state.confirm_delete = selected
    st.rerun()
