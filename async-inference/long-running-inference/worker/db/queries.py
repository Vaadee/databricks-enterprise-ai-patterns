"""
All SQL for the worker lives here. No inline SQL in worker.py or services.
All functions are synchronous — the worker is a plain Python script.
"""

import logging
import psycopg

logger = logging.getLogger(__name__)


def mark_streaming(conn: psycopg.Connection, job_id: str) -> None:
    """
    Transition RUNNING → STREAMING on first token.
    Guard: only updates if status is currently RUNNING to prevent invalid transitions
    (e.g. if the reconciler already reset this job or another worker beat us to it).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'STREAMING', updated_at = now()
            WHERE job_id = %s::uuid AND status = 'RUNNING'
            """,
            (job_id,),
        )
        if cur.rowcount == 0:
            logger.warning(
                "mark_streaming: job_id=%s was not in RUNNING state — skipped", job_id
            )
    conn.commit()


def write_chunk(conn: psycopg.Connection, job_id: str, index: int, content: str) -> None:
    """
    Insert a buffered token chunk. ON CONFLICT DO NOTHING handles the case where a
    retried worker writes the same chunk_index again — the UNIQUE(job_id, chunk_index)
    constraint prevents corruption, and the existing chunk is kept unchanged.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_chunks (job_id, chunk_index, content)
            VALUES (%s::uuid, %s, %s)
            ON CONFLICT (job_id, chunk_index) DO NOTHING
            """,
            (job_id, index, content),
        )
    conn.commit()


def mark_done(
    conn: psycopg.Connection,
    job_id: str,
    full_text: str,
    total_tokens: int,
    prompt_tokens: int,
    latency_ms: int,
) -> None:
    """
    Write final result and transition to DONE. Both statements run in the same
    transaction — if the process crashes before commit, both are rolled back.
    Guard: only updates status if not already in a terminal state (prevents
    overwriting DONE/FAILED if the job was somehow double-processed).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_results (job_id, full_text, total_tokens, prompt_tokens, latency_ms)
            VALUES (%s::uuid, %s, %s, %s, %s)
            """,
            (job_id, full_text, total_tokens, prompt_tokens, latency_ms),
        )
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'DONE', updated_at = now()
            WHERE job_id = %s::uuid
              AND status NOT IN ('DONE', 'FAILED')
            """,
            (job_id,),
        )
        if cur.rowcount == 0:
            logger.warning(
                "mark_done: job_id=%s already in terminal state — status update skipped", job_id
            )
    conn.commit()


def mark_failed(conn: psycopg.Connection, job_id: str, error: str) -> None:
    """
    Mark job as FAILED with an error message.
    Guard: never overwrites a DONE job (race where worker fails after marking DONE).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'FAILED', error_msg = %s, updated_at = now()
            WHERE job_id = %s::uuid
              AND status NOT IN ('DONE', 'FAILED')
            """,
            (error, job_id),
        )
        if cur.rowcount == 0:
            logger.warning(
                "mark_failed: job_id=%s already in terminal state — skipped", job_id
            )
    conn.commit()
