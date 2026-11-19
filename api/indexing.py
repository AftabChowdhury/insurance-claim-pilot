"""Background indexing of policy documents inside the API process.

- Every successful upload is queued automatically (POLICY_AUTO_INDEX=true).
- On startup a resync queues every document that is not in sync: new, interrupted by a restart, indexed with
  old chunk settings, or missing from the index (e.g. after `ingest_policies --recreate`). Failed documents are
  retried only by an explicit resync / re-index from the Admin panel.
- One document at a time (vision + embedding rate limits; SQLite), each document at most once in the queue.
"""
import asyncio
import logging

from core import db
from core.config import get_settings
from ingest.pipeline import QUEUED, documents_to_index, process_policy_doc

log = logging.getLogger(__name__)


class IndexWorker:
    def __init__(self):
        self.queue: asyncio.Queue[tuple[str, bool, str]] = asyncio.Queue()
        self.pending: set[str] = set()
        self.current: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="policy-index-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def enqueue(self, doc_id: str, reextract: bool = False, actor: str = "system") -> bool:
        """Queue a document; False if it is already waiting (a running one may be queued again)."""
        if doc_id in self.pending:
            return False
        self.pending.add(doc_id)
        db.update_policy_doc(doc_id, status=QUEUED, stage=None, error=None)
        self.queue.put_nowait((doc_id, reextract, actor))
        return True

    async def resync(self, include_failed: bool = True, actor: str = "system") -> list[dict]:
        """Queue every document whose index content is out of sync. Returns [{doc_id, reason}]."""
        todo = await asyncio.to_thread(documents_to_index, include_failed)
        queued = []
        for doc_id, reason in todo:
            if doc_id != self.current and self.enqueue(doc_id, actor=actor):
                queued.append({"doc_id": doc_id, "reason": reason})
        if queued:
            log.info("Resync queued %d document(s): %s", len(queued),
                     ", ".join(f"{q['doc_id']} ({q['reason']})" for q in queued))
        return queued

    def state(self) -> dict:
        return {"current": self.current, "queued": self.queue.qsize()}

    async def _run(self) -> None:
        while True:
            doc_id, reextract, actor = await self.queue.get()
            self.pending.discard(doc_id)
            self.current = doc_id
            try:
                await process_policy_doc(doc_id, reextract=reextract, actor=actor)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - status "failed" + error are stored by process_policy_doc
                pass
            finally:
                self.current = None
                self.queue.task_done()


async def startup_resync(worker: IndexWorker) -> None:
    """Runs in the background after API start; a missing Search configuration must not stop the API."""
    if not get_settings().policy_auto_index:
        return
    try:
        await worker.resync(include_failed=False)
    except Exception as e:  # noqa: BLE001
        log.warning("Policy index resync on startup skipped: %s", e)
