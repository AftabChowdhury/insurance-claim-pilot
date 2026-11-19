"""Policy chat API.

POST   /chat                    one turn → answer, status, citations, evaluation, timeline
POST   /chat/stream             same, as Server-Sent Events (step events, then the answer)
GET    /chat/{session_id}/history
DELETE /chat/{session_id}       forget the conversation

status: answered | partial | restricted | not_found | blocked | fallback
"""
import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.config import get_settings

from .state import turn_input

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])
SESSION_ID = r"^[A-Za-z0-9_-]{8,64}$"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(None, pattern=SESSION_ID)
    policy_type: Literal["home", "auto"] | None = None


class RateLimiter:
    """Messages per session per minute (in memory) – makes brute-force probing of confidential topics harder."""

    def __init__(self):
        self.calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, session_id: str) -> None:
        limit = get_settings().chat_rate_limit_per_min
        now = time.monotonic()
        q = self.calls[session_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, f"Too many messages – at most {limit} per minute. Please wait a moment.")
        q.append(now)


_limiter = RateLimiter()
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def config_for(session_id: str) -> dict:
    return {"configurable": {"thread_id": f"chat-{session_id}"}, "run_name": "policy-chat",
            "tags": ["claimpilot", "chat"], "metadata": {"session_id": session_id}}


def _graph(request: Request):
    return request.app.state.chat_graph


def _prepare(body: ChatRequest) -> str:
    session_id = body.session_id or uuid.uuid4().hex
    _limiter.check(session_id)
    return session_id


def _response(session_id: str, values: dict) -> dict:
    result = values.get("result") or {}
    if not result:
        raise HTTPException(500, "The chat workflow did not produce an answer")
    return {"session_id": session_id, **result, "timeline": values.get("events", [])}


@router.post("")
async def chat(body: ChatRequest, request: Request):
    """Ask a question about the policies. Omit session_id to start a new conversation."""
    session_id = _prepare(body)
    async with _locks[session_id]:
        values = await _graph(request).ainvoke(turn_input(session_id, body.message.strip(), body.policy_type),
                                               config_for(session_id))
    return _response(session_id, values)


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    """Server-Sent Events: `step` per agent step (timeline), then `answer` with the full response."""
    session_id = _prepare(body)
    graph = _graph(request)

    async def gen():
        async with _locks[session_id]:
            yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"
            try:
                async for update in graph.astream(turn_input(session_id, body.message.strip(), body.policy_type),
                                                  config_for(session_id), stream_mode="updates"):
                    for node_update in update.values():
                        for e in (node_update or {}).get("events") or []:
                            yield f"event: step\ndata: {json.dumps(e, default=str)}\n\n"
                snap = await graph.aget_state(config_for(session_id))
                yield f"event: answer\ndata: {json.dumps(_response(session_id, snap.values), default=str)}\n\n"
            except Exception as e:  # noqa: BLE001 - report to the client instead of cutting the stream
                log.exception("Chat stream failed for %s", session_id)
                yield f"event: error\ndata: {json.dumps({'detail': f'{type(e).__name__}: {e}'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/{session_id}/history")
async def chat_history(session_id: str, request: Request):
    if not re.fullmatch(SESSION_ID[1:-1], session_id):
        raise HTTPException(422, "Invalid session id")
    snap = await _graph(request).aget_state(config_for(session_id))
    return {"session_id": session_id, "history": (snap.values or {}).get("history", [])}


@router.delete("/{session_id}")
async def chat_delete(session_id: str, request: Request):
    if not re.fullmatch(SESSION_ID[1:-1], session_id):
        raise HTTPException(422, "Invalid session id")
    graph = _graph(request)
    await graph.checkpointer.adelete_thread(f"chat-{session_id}")
    _locks.pop(session_id, None)
    return {"session_id": session_id, "deleted": True}
