"""Runs graph executions in the background and reports their status."""
import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


def config_for(claim_id: str) -> dict:
    # One LangGraph thread per claim; the checkpointer stores its state so it can pause and resume.
    return {"configurable": {"thread_id": claim_id}, "run_name": f"claim-{claim_id}", "tags": ["claimpilot"]}


class Runner:
    def __init__(self, graph):
        self.graph = graph
        self.tasks: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}

    def start(self, claim_id: str, payload: Any) -> None:
        """payload is the initial state, or Command(resume=...) to continue after human review."""
        if self.is_running(claim_id):
            raise RuntimeError(f"Claim {claim_id} is already running")
        self.errors.pop(claim_id, None)
        self.tasks[claim_id] = asyncio.create_task(self._run(claim_id, payload))

    async def _run(self, claim_id: str, payload: Any) -> None:
        try:
            async for _ in self.graph.astream(payload, config_for(claim_id), stream_mode="updates"):
                pass
        except Exception as e:  # noqa: BLE001
            log.exception("Run failed for %s", claim_id)
            self.errors[claim_id] = f"{type(e).__name__}: {e}"

    def is_running(self, claim_id: str) -> bool:
        t = self.tasks.get(claim_id)
        return t is not None and not t.done()

    async def status(self, claim_id: str) -> tuple[str, dict, dict | None]:
        """Returns (status, state values, interrupt payload)."""
        snap = await self.graph.aget_state(config_for(claim_id))
        values = snap.values or {}
        review = None
        for task in snap.tasks or []:
            if task.interrupts:
                review = task.interrupts[0].value
        if self.is_running(claim_id):
            status = "running"
        elif claim_id in self.errors:
            status = "failed"
        elif review is not None:
            status = "awaiting_review"
        elif values.get("final"):
            status = "completed"
        elif not values:
            status = "not_started"
        else:
            status = "failed"  # stopped without finishing (e.g. server restarted mid-run)
        return status, values, review
