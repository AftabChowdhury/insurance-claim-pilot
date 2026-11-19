"""ClaimPilot FastAPI backend.

Run:  uv run uvicorn api.main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from core import db
from core.config import get_settings
from core.domain import LOSS_TYPES, policy_type_for

from . import admin, auth, customer
from .chat.graph import build_chat_graph
from .chat.router import router as chat_router
from .documents import save_uploads
from .graph.graph import build_graph
from .indexing import IndexWorker, startup_resync
from .models import ClaimCreated, ClaimView, ReviewDecision
from .runner import Runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    s.policy_upload_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()
    app.state.indexer = IndexWorker()
    app.state.indexer.start()
    resync = asyncio.create_task(startup_resync(app.state.indexer))
    async with AsyncSqliteSaver.from_conn_string(str(s.checkpoint_path)) as saver:
        app.state.runner = Runner(build_graph(checkpointer=saver))
        app.state.chat_graph = build_chat_graph(checkpointer=saver)
        yield
    resync.cancel()
    await app.state.indexer.stop()


app = FastAPI(title="ClaimPilot API", version="0.1.0", lifespan=lifespan,
              description="Multi-agent insurance claims copilot and policy chat "
                          "(LangGraph + Azure AI Search + MCP)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(admin.chat_audit_router)
app.include_router(admin.customers_router)
app.include_router(auth.customer_router)
app.include_router(customer.router)
app.include_router(chat_router)


def runner(request: Request) -> Runner:
    return request.app.state.runner


# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/loss-types")
def loss_types():
    return {k: label for k, (_, label) in LOSS_TYPES.items()}


@app.post("/claims", response_model=ClaimCreated, status_code=202)
async def submit_claim(
    request: Request,
    policy_number: str = Form(..., min_length=3, max_length=40),
    loss_type: str = Form(...),
    amount: float = Form(..., gt=0),
    loss_date: date = Form(...),
    description: str = Form(..., min_length=10, max_length=5000),
    files: list[UploadFile] = File(...),
):
    """Submit a claim with documents. Starts the multi-agent workflow in the background."""
    if loss_type not in LOSS_TYPES:
        raise HTTPException(422, f"loss_type must be one of {list(LOSS_TYPES)}")
    if loss_date > date.today():
        raise HTTPException(422, "loss_date cannot be in the future")

    claim_id = db.new_claim_id()
    docs = await save_uploads(claim_id, files)
    db.create_claim({"id": claim_id, "policy_number": policy_number.strip(), "loss_type": loss_type, "amount": amount,
                     "loss_date": loss_date.isoformat(), "description": description.strip(),
                     "documents": [{k: d[k] for k in ("name", "size", "extraction")} for d in docs]})

    initial_state = {
        "claim_id": claim_id, "policy_number": policy_number.strip(), "loss_type": loss_type,
        "policy_type": policy_type_for(loss_type), "amount": amount, "loss_date": loss_date.isoformat(),
        "description": description.strip(), "documents": docs, "retries": 0, "events": [],
    }
    runner(request).start(claim_id, initial_state)
    return ClaimCreated(claim_id=claim_id, thread_id=claim_id, status="running",
                        documents=[{k: d[k] for k in ("name", "size", "extraction")} for d in docs])


@app.get("/claims")
def list_claims(status: str | None = None, limit: int = 50):
    return db.list_claims(status, limit)


@app.get("/claims/{claim_id}", response_model=ClaimView)
async def get_claim(claim_id: str, request: Request):
    record = db.get_claim(claim_id)
    if not record:
        raise HTTPException(404, "Claim not found")
    r = runner(request)
    status, v, review = await r.status(claim_id)
    return ClaimView(
        claim_id=claim_id, status=status, error=r.errors.get(claim_id), record=record,
        events=v.get("events", []), review_request=review,
        guard_in=v.get("guard_in"), claim=v.get("claim"),
        ranked=[{k: c.get(k) for k in ("clause_id", "title", "section", "reranker_score", "score", "content")}
                for c in v.get("ranked", [])],
        coverage=v.get("coverage"), fraud=v.get("fraud"), letter=v.get("letter"), evaluation=v.get("evaluation"),
        guard_out=v.get("guard_out"), human=v.get("human"), final=v.get("final"), retries=v.get("retries", 0),
    )


@app.get("/claims/{claim_id}/events")
async def stream_events(claim_id: str, request: Request):
    """Server-Sent Events: one message per workflow step, until the run stops or pauses."""
    if not db.get_claim(claim_id):
        raise HTTPException(404, "Claim not found")
    r = runner(request)

    async def gen():
        sent = 0
        while True:
            status, v, _ = await r.status(claim_id)
            events = v.get("events", [])
            for e in events[sent:]:
                yield f"event: step\ndata: {json.dumps(e, default=str)}\n\n"
            sent = len(events)
            if status != "running":
                yield f"event: status\ndata: {json.dumps({'status': status})}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/claims/{claim_id}/review")
async def review_claim(claim_id: str, body: ReviewDecision, request: Request):
    """Adjuster decision. Resumes the paused LangGraph run (interrupt → Command(resume=...))."""
    r = runner(request)
    status, _, _ = await r.status(claim_id)
    if status != "awaiting_review":
        raise HTTPException(409, f"Claim is not awaiting review (status: {status})")
    r.start(claim_id, Command(resume=body.model_dump()))
    return {"claim_id": claim_id, "status": "running"}


@app.get("/claims/{claim_id}/letter", response_class=PlainTextResponse)
def get_letter(claim_id: str):
    record = db.get_claim(claim_id)
    if not record:
        raise HTTPException(404, "Claim not found")
    if not record.get("letter"):
        raise HTTPException(404, "No decision letter yet")
    return record["letter"]


@app.get("/claims/{claim_id}/audit")
def get_audit(claim_id: str):
    return db.get_audit(claim_id)


@app.get("/graph", response_class=PlainTextResponse)
def graph_mermaid(request: Request, which: str = "claims"):
    """Mermaid diagram of an agent graph (paste into mermaid.live). which = claims | chat"""
    graph = request.app.state.chat_graph if which == "chat" else runner(request).graph
    return graph.get_graph().draw_mermaid()
