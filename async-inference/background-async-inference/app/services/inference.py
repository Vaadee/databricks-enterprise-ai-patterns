"""
Background inference runner.

Each submitted job becomes an asyncio.create_task(inference.run(...)) call.
Concurrency is bounded by MAX_CONCURRENT_INFERENCES (asyncio.Semaphore).

The entire inference (DB + OpenAI streaming) runs in asyncio.to_thread so it
never blocks the event loop. The connection is created inside the thread via
db_manager.get_sync_conn() to avoid psycopg3 thread-crossing violations.
"""

import asyncio
import logging
import os

from db.connection import db_manager
from db import queries

logger = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        limit = int(os.environ.get("MAX_CONCURRENT_INFERENCES", "20"))
        _semaphore = asyncio.Semaphore(limit)
    return _semaphore


async def run(job_id: str, payload: dict) -> None:
    """
    Entry point called via asyncio.create_task() from the submit endpoint.
    Acquires the semaphore for backpressure, then runs inference in a thread.
    """
    async with get_semaphore():
        try:
            await asyncio.to_thread(_run_sync, job_id, payload)
        except Exception:
            logger.exception("Background inference task raised for job_id=%s", job_id)


def _run_sync(job_id: str, payload: dict) -> None:
    """
    Runs entirely in a thread pool thread — creates its own DB connection
    (no thread-crossing) and delegates to the sync streaming service.
    """
    from services.azure_openai import stream_completion

    worker_id = f"app-bg-{job_id[:8]}"
    conn = db_manager.get_sync_conn()
    try:
        row = queries.claim_job_by_id(conn, job_id, worker_id)
        if row is None:
            logger.info("job_id=%s not PENDING — skipping (already claimed or completed)", job_id)
            return
        stream_completion(conn, job_id, row["payload"])
    finally:
        conn.close()
