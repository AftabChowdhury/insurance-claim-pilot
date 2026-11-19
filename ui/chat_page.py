"""Policy Chat: ask questions about the insurance policies. Answers cite policy clauses, are checked by an
evaluator before they are shown, and never include content from confidential documents."""
import json
import uuid

import requests
import streamlit as st

from common import API, api

NODE_LABEL = {
    "guard_input": "Input guardrails", "rewrite_query": "Query agent", "hybrid_search": "Hybrid search",
    "confidential_probe": "Confidential check", "semantic_rank": "Semantic ranker", "build_context": "Context builder",
    "access_check": "Access control", "answer_agent": "Answer agent", "evaluator_agent": "Evaluator",
    "prepare_retry": "Retry", "guard_output": "Output guardrails", "respond": "Response",
}
ICON = {"done": "✅", "warn": "⚠️", "fail": "❌", "running": "⏳"}
POLICY_FILTER = {"All policies": None, "Home": "home", "Auto": "auto"}
EXAMPLES = ["Is water damage from a burst pipe covered?", "What deductible applies to water damage?",
            "Is my car covered if it is stolen while unlocked?"]


def new_conversation() -> None:
    old = st.session_state.get("chat_session")
    if old and st.session_state.get("chat_messages"):
        api("DELETE", f"/chat/{old}")
    st.session_state.chat_session = uuid.uuid4().hex
    st.session_state.chat_messages = []


if "chat_session" not in st.session_state:
    new_conversation()

with st.sidebar:
    st.title("💬 Policy Chat")
    st.caption("Ask about your insurance policies. Answers cite the policy clauses they are based on.")
    policy_label = st.selectbox("Policy", list(POLICY_FILTER))
    if st.button("New conversation", use_container_width=True):
        new_conversation()
        st.rerun()
    st.caption("🔒 Information from confidential documents is never shown.")


def timeline(events: list[dict]) -> None:
    for e in events:
        st.markdown(f"{ICON.get(e.get('status'), '•')} **{NODE_LABEL.get(e['node'], e['node'])}** — {e['message']}")


def render_answer(m: dict, idx: int) -> None:
    status, text = m.get("status"), m.get("answer", "")
    if status == "restricted":
        st.warning(text, icon="🔒")
    elif status == "blocked":
        st.error(text, icon="🛡️")
    elif status in ("not_found", "fallback"):
        st.info(text, icon="ℹ️")
    else:
        st.markdown(text)

    ev = m.get("evaluation") or {}
    if status in ("answered", "partial") and ev:
        attempts = m.get("attempts", 1)
        st.caption(f"✔ verified · faithfulness {ev['faithfulness']:.2f} · relevance {ev['answer_relevance']:.2f} · "
                   f"citations {ev['citation_coverage']:.2f} · {attempts} attempt{'s' if attempts != 1 else ''}")
    elif status == "fallback" and ev:
        st.caption(f"✖ no answer passed the evaluation after {m.get('attempts', 0)} attempt(s)")

    if cites := m.get("citations"):
        with st.expander(f"Sources ({len(cites)})"):
            for c in cites:
                page = f" · p. {c['page']}" if c.get("page") else ""
                st.markdown(f"**[Clause {c['clause_id']}] {c['title']}**  \n:gray[{c.get('source')}{page}]")
                st.markdown(c.get("content") or "")
    if m.get("timeline"):
        with st.expander("How this answer was made"):
            if m.get("standalone_question"):
                st.caption(f"Question understood as: “{m['standalone_question']}”")
            timeline(m["timeline"])
    if m.get("suggestions") and idx == len(st.session_state.chat_messages) - 1:
        cols = st.columns(len(m["suggestions"]))
        for col, s in zip(cols, m["suggestions"]):
            if col.button(s, key=f"sugg_{idx}_{s}"):
                st.session_state.pending_prompt = s
                st.rerun()


def ask(question: str) -> dict | None:
    """Stream one turn: agent steps appear live in a status box, then the answer."""
    body = {"message": question, "session_id": st.session_state.chat_session,
            "policy_type": POLICY_FILTER[policy_label]}
    steps: list[dict] = []
    with st.status("Thinking…", expanded=False) as box:
        try:
            with requests.post(f"{API}/chat/stream", json=body, stream=True, timeout=300) as r:
                if r.status_code >= 400:
                    try:
                        detail = r.json().get("detail")
                    except ValueError:
                        detail = r.text
                    box.update(label="Error", state="error")
                    st.error(f"API error {r.status_code}: {detail}")
                    return None
                name = None
                for line in r.iter_lines(decode_unicode=True):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        if name == "step":
                            steps.append(data)
                            box.update(label=f"{NODE_LABEL.get(data['node'], data['node'])}…")
                            box.markdown(f"{ICON.get(data.get('status'), '•')} "
                                         f"**{NODE_LABEL.get(data['node'], data['node'])}** — {data['message']}")
                        elif name == "answer":
                            box.update(label=f"Done · {data['status']}", state="complete")
                            return data
                        elif name == "error":
                            box.update(label="Error", state="error")
                            st.error(data.get("detail"))
                            return None
        except requests.ConnectionError:
            box.update(label="Error", state="error")
            st.error(f"Cannot reach the API at {API}. Start it with: uv run uvicorn api.main:app --port 8000")
            return None
    return None


st.title("💬 Policy Chat")
if not st.session_state.chat_messages:
    st.markdown("Ask anything about your **home** or **auto** policy – what is covered, deductibles, limits, "
                "exclusions or what to do after a loss.")
    cols = st.columns(len(EXAMPLES))
    for col, ex in zip(cols, EXAMPLES):
        if col.button(ex, use_container_width=True):
            st.session_state.pending_prompt = ex
            st.rerun()

for i, m in enumerate(st.session_state.chat_messages):
    with st.chat_message(m["role"]):
        if m["role"] == "user":
            st.markdown(m["content"])
        else:
            render_answer(m, i)

prompt = st.chat_input("Ask about your policy…") or st.session_state.pop("pending_prompt", None)
if prompt:
    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        res = ask(prompt)
    if res:
        st.session_state.chat_session = res["session_id"]
        st.session_state.chat_messages.append({"role": "assistant", **res})
    else:
        st.session_state.chat_messages.pop()  # let the user try again
    st.rerun()
