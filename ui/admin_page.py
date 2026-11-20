"""Admin Panel: static login, upload policy documents (.pdf/.doc/.docx), confidential flag, indexing status,
re-index, chunk preview, delete, the log of restricted / blocked policy-chat requests, the claims review
(agent timeline + adjuster review) and the customer list."""
from pathlib import Path

import streamlit as st

from claims_review import claims_review
from common import admin_api, admin_logout, api

ALLOWED_TYPES = ["pdf", "doc", "docx"]
POLICY_TYPES = {"Detect from document": "", "Home": "home", "Auto": "auto", "General": "general"}
ROOT = Path(__file__).resolve().parent.parent
STATUS_BADGE = {
    "uploaded": ":gray-badge[uploaded]", "queued": ":blue-badge[queued]",
    "processing": ":orange-badge[processing]", "indexed": ":green-badge[indexed]", "failed": ":red-badge[failed]",
}
ACTIVE = ("queued", "processing")


logout = admin_logout


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

_waiting = sum(len(admin_api("GET", f"/claims?status={s}&limit=200") or [])
               for s in ("AWAITING_REVIEW", "FLAGGED_SECURITY"))
tab_docs, tab_claims, tab_customers = st.tabs(
    ["📄 Policy documents", f"🧑‍⚖️ Claims review ({_waiting} waiting)", "👥 Customers"])

# ---------------------------------------------------------------------------
# Claims review and Customers (rendered first: the documents tab below may st.stop() early)
# ---------------------------------------------------------------------------
with tab_claims:
    claims_review()

with tab_customers:
    customers = admin_api("GET", "/admin/customers") or []
    search = st.text_input("Search customers", placeholder="Name, customer ID or email")
    if search:
        q = search.strip().lower()
        customers = [c for c in customers if any(q in (c.get(k) or "").lower() for k in ("name", "id", "email"))]
    st.caption(f"{len(customers)} customer(s) · demo login: customer email + password `user`")
    st.dataframe(
        [{"Customer name": c["name"], "Customer ID": c["id"], "Email": c["email"], "Phone": c["phone"]}
         for c in customers],
        hide_index=True, width="stretch")

with tab_docs:
    # ---------------------------------------------------------------------------
    # Upload
    # ---------------------------------------------------------------------------
    st.subheader("Upload policy documents")
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0
    files = st.file_uploader("PDF or Word (.doc, .docx) – you can select several files", type=ALLOWED_TYPES,
                             accept_multiple_files=True, key=f"uploader_{st.session_state.uploader_key}")
    policy_label = st.selectbox("Policy type", list(POLICY_TYPES), help="Applies to all selected files. "
                                "'Detect' uses front matter or the file name (home…, auto…), else 'general'.")
    confidential = st.checkbox("🔒 Confidential", help="Applies to all selected files. Confidential documents are "
                               "indexed but never shown to users in the policy chat; the chat tells the user the "
                               "information is confidential instead.")
    if st.button("Upload", type="primary", disabled=not files):
        res = admin_api("POST", "/admin/policies",
                        data={"policy_type": POLICY_TYPES[policy_label], "confidential": str(confidential).lower()},
                        files=[("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files])
        if res:
            st.session_state.upload_results = res
            st.session_state.uploader_key += 1  # clear the uploader
            st.rerun()

    for r in st.session_state.pop("upload_results", []):
        if r["status"] == "queued":
            st.success(f"Uploaded **{r['filename']}** – indexing started in the background")
        elif r["status"] == "uploaded":
            st.success(f"Uploaded **{r['filename']}** (automatic indexing is off – use Re-index)")
        else:
            st.error(f"Rejected **{r['filename']}**: {r['error']}")

    st.divider()


    # ---------------------------------------------------------------------------
    # Documents list (refreshes itself while documents are being indexed)
    # ---------------------------------------------------------------------------
    def status_cell(d: dict) -> str:
        badge = STATUS_BADGE.get(d["status"], d["status"])
        if d["status"] == "processing" and d.get("stage"):
            badge += f" {d['stage'].replace('_', ' ')}…"
        if d["status"] == "indexed" and d.get("chunk_count") is not None:
            badge += f" {d['chunk_count']} chunks"
        return badge


    def documents_list(was_active: bool) -> None:
        docs = admin_api("GET", "/admin/policies") or []
        active = any(d["status"] in ACTIVE for d in docs)
        if was_active and not active:
            st.rerun(scope="app")  # indexing finished: a full rerun stops the auto-refresh
        head_l, head_m, head_r = st.columns([5, 1, 1])
        head_l.subheader("Uploaded documents")
        if head_m.button("Refresh", use_container_width=True):
            st.rerun(scope="app")
        if head_r.button("Resync", use_container_width=True,
                         help="Index everything that is not in sync: new, failed, outdated or missing documents"):
            r = admin_api("POST", "/admin/policies/resync")
            if r is not None:
                st.toast(f"Queued {len(r['queued'])} document(s)" if r["queued"] else "Everything is in sync")
                st.rerun(scope="app")
        if not docs:
            st.info("No policy documents uploaded yet.")
            return
        if st.session_state.get("only_confidential"):
            docs = [d for d in docs if d.get("confidential")]
            if not docs:
                st.info("No confidential documents.")
                return
        if active:
            st.caption("Indexing in progress – this list refreshes automatically.")

        cols = [4, 1, 1, 3, 2, 1, 1, 1]
        hdr = st.columns(cols)
        for c, label in zip(hdr, ["File", "Type", "Policy", "Index status", "Uploaded at (UTC)", "", "", ""]):
            if label:
                c.markdown(f"**{label}**")
        for d in docs:
            row = st.columns(cols, vertical_alignment="center")
            lock = " :red-badge[🔒 confidential]" if d.get("confidential") else ""
            row[0].markdown(f"{d['filename']}{lock}  \n:gray[{human_size(d['size_bytes'])} · {d['uploaded_by']}]")
            row[1].write(d["file_type"].upper())
            row[2].write(d.get("policy_type") or "auto-detect")
            row[3].markdown(status_cell(d))
            if d["status"] == "failed" and d.get("error"):
                row[3].caption(d["error"][:200])
            row[4].write(d["uploaded_at"].replace("T", " ").split("+")[0])
            is_conf = bool(d.get("confidential"))
            if row[5].button("🔓" if is_conf else "🔒", key=f"conf_{d['id']}",
                             help=("Make public (visible in the policy chat)" if is_conf
                                   else "Mark as confidential (hidden from the policy chat)")):
                r = admin_api("PATCH", f"/admin/policies/{d['id']}", json={"confidential": not is_conf})
                if r:
                    st.session_state.conf_toast = (
                        f"{d['filename']} is now {'confidential' if r['confidential'] else 'public'}"
                        f" · {r['chunks_updated']} chunk(s) updated in the index")
                    st.session_state.conf_error = r.get("error")
                st.rerun(scope="app")
            if row[6].button("🔄", key=f"idx_{d['id']}", help=f"Re-index {d['filename']}",
                             disabled=d["status"] in ACTIVE):
                admin_api("POST", f"/admin/policies/{d['id']}/index")
                st.rerun(scope="app")
            if row[7].button("🗑", key=f"del_{d['id']}", help=f"Delete {d['filename']}"):
                st.session_state.confirm_delete = [d["id"]]
                st.rerun(scope="app")


    docs = admin_api("GET", "/admin/policies") or []
    by_id = {d["id"]: d for d in docs}
    confirm = st.session_state.get("confirm_delete")  # list of doc ids awaiting confirmation

    if confirm:
        names = ", ".join(by_id[i]["filename"] for i in confirm if i in by_id)
        with st.container(border=True):
            st.warning(f"Delete {len(confirm)} document(s) and remove them from the search index? {names}")
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

    if msg := st.session_state.pop("conf_toast", None):
        st.toast(msg, icon="🔒")
    if err := st.session_state.pop("conf_error", None):
        st.warning(err)
    st.checkbox("Show confidential documents only", key="only_confidential")
    is_active = any(d["status"] in ACTIVE for d in docs)
    st.fragment(documents_list, run_every=3 if is_active else None)(is_active)

    if not docs:
        st.stop()

    st.markdown("")
    selected = st.multiselect("Select documents to delete", list(by_id), format_func=lambda i: by_id[i]["filename"])
    if st.button("Delete selected", disabled=not selected):
        st.session_state.confirm_delete = selected
        st.rerun()

    # ---------------------------------------------------------------------------
    # Policy chat: restricted / blocked requests
    # ---------------------------------------------------------------------------
    st.divider()
    with st.expander("🔒 Policy chat – restricted and blocked requests"):
        rows = admin_api("GET", "/admin/chat/audit") or []
        if not rows:
            st.caption("No restricted or blocked chat requests yet.")
        for a in rows:
            kind = ":orange-badge[restricted]" if a["action"] == "chat_restricted" else ":red-badge[blocked]"
            st.markdown(f"{kind} :gray[{a['ts'].replace('T', ' ').split('+')[0]} · {a['actor']}]  \n{a['detail']}")

    # ---------------------------------------------------------------------------
    # Chunk preview
    # ---------------------------------------------------------------------------
    st.divider()
    st.subheader("Chunk preview")
    indexed = [i for i, d in by_id.items() if d["status"] == "indexed"]
    if not indexed:
        st.caption("Chunks appear here once a document is indexed.")
        st.stop()
    doc_id = st.selectbox("Document", indexed, format_func=lambda i: by_id[i]["filename"])
    chunks = admin_api("GET", f"/admin/policies/{doc_id}/chunks") or []
    counts = {t: sum(c["chunk_type"] == t for c in chunks) for t in ("text", "table", "figure")}
    if by_id[doc_id].get("confidential"):
        st.caption("🔒 Confidential document – these chunks are never returned to the policy chat.")
    st.caption(f"{len(chunks)} chunks · {counts['text']} text · {counts['table']} table · "
               f"{counts['figure']} figure · policy type {chunks[0]['policy_type'] if chunks else '-'}")
    for c in chunks:
        page = f" · p. {c['page']}" if c.get("page") else ""
        with st.expander(f"[{c['clause_id']}] {c['title']} · {c['chunk_type']} · {c['tokens']} tokens{page}"):
            for ref in c.get("image_refs") or []:
                img = ROOT / ref
                if img.is_file():
                    st.image(str(img), width=360)
            st.markdown(c["content"])
