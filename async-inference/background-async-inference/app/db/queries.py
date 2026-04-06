"""
All SQL for the FastAPI app lives here. No inline SQL in routers or services.
Functions are synchronous (called via asyncio.to_thread inside the session context,
or directly inside a thread pool thread for background inference tasks).
"""

import json
import logging
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


def insert_job(conn: psycopg.Connection, payload: dict) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO job_requests (payload)
            VALUES (%s::jsonb)
            RETURNING job_id, status, created_at
            """,
            (json.dumps(payload),),
        )
        return cur.fetchone()


def get_job_status(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT job_id, status, created_at, updated_at, error_msg AS error
            FROM job_requests
            WHERE job_id = %s::uuid
            """,
            (job_id,),
        )
        return cur.fetchone()


def get_job_result(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        # Fetch request row first
        cur.execute(
            """
            SELECT job_id, status, error_msg
            FROM job_requests
            WHERE job_id = %s::uuid
            """,
            (job_id,),
        )
        req = cur.fetchone()
        if not req:
            return None

        status = req["status"]

        if status == "DONE":
            cur.execute(
                """
                SELECT r.job_id, rq.status, r.full_text AS result,
                       r.total_tokens, r.latency_ms
                FROM job_results r
                JOIN job_requests rq ON rq.job_id = r.job_id
                WHERE r.job_id = %s::uuid
                """,
                (job_id,),
            )
            return cur.fetchone()

        if status == "STREAMING":
            cur.execute(
                """
                SELECT string_agg(content, '' ORDER BY chunk_index) AS partial
                FROM job_chunks
                WHERE job_id = %s::uuid
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return {
                "job_id": req["job_id"],
                "status": status,
                "partial": row["partial"] if row else None,
                "note": "inference in progress — poll again for final result",
            }

        if status == "FAILED":
            return {
                "job_id": req["job_id"],
                "status": status,
                "note": req["error_msg"],
            }

        # PENDING or RUNNING
        return {
            "job_id": req["job_id"],
            "status": status,
            "note": "job queued — poll /status for updates",
        }


# ── Background inference query functions ──────────────────────────────────────
# Called from inside asyncio.to_thread (background inference tasks).
# Each function expects a connection created in the same thread via get_sync_conn().

def claim_job_by_id(conn: psycopg.Connection, job_id: str, worker_id: str) -> dict | None:
    """Atomically claim a specific job if PENDING. Returns row with payload or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'RUNNING', claimed_by = %s, updated_at = now()
            WHERE job_id = %s::uuid AND status = 'PENDING'
            RETURNING job_id, payload
            """,
            (worker_id, job_id),
        )
        conn.commit()
        return cur.fetchone()


def mark_streaming(conn: psycopg.Connection, job_id: str) -> None:
    """RUNNING → STREAMING on first token. Guard: only transitions from RUNNING."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_requests SET status = 'STREAMING', updated_at = now()
            WHERE job_id = %s::uuid AND status = 'RUNNING'
            """,
            (job_id,),
        )
        if cur.rowcount == 0:
            logger.warning("mark_streaming: job_id=%s not in RUNNING state — skipped", job_id)
    conn.commit()


def write_chunk(conn: psycopg.Connection, job_id: str, index: int, content: str) -> None:
    """Insert a token chunk. ON CONFLICT DO NOTHING handles retried tasks safely."""
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
    """Write result + transition to DONE. Guard: skips if already terminal."""
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
            UPDATE job_requests SET status = 'DONE', updated_at = now()
            WHERE job_id = %s::uuid AND status NOT IN ('DONE', 'FAILED')
            """,
            (job_id,),
        )
        if cur.rowcount == 0:
            logger.warning("mark_done: job_id=%s already in terminal state — skipped", job_id)
    conn.commit()


def mark_failed(conn: psycopg.Connection, job_id: str, error: str) -> None:
    """Mark FAILED. Guard: never overwrites a DONE job."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'FAILED', error_msg = %s, updated_at = now()
            WHERE job_id = %s::uuid AND status NOT IN ('DONE', 'FAILED')
            """,
            (error, job_id),
        )
        if cur.rowcount == 0:
            logger.warning("mark_failed: job_id=%s already in terminal state — skipped", job_id)
    conn.commit()


def reset_in_flight_jobs(conn: psycopg.Connection) -> list[dict]:
    """
    On app startup: reset any jobs left RUNNING or STREAMING from a previous app
    instance back to PENDING, and delete their partial chunks.
    Returns list of {job_id, payload} rows to re-spawn as background tasks.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'PENDING', claimed_by = NULL, updated_at = now()
            WHERE status IN ('RUNNING', 'STREAMING')
            RETURNING job_id, payload
            """,
        )
        rows = cur.fetchall()
    if rows:
        job_ids = [str(r["job_id"]) for r in rows]
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM job_chunks WHERE job_id = ANY(%s::uuid[])",
                (job_ids,),
            )
    conn.commit()
    return rows
