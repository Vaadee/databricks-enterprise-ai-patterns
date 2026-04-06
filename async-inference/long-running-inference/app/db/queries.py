"""
All SQL for the FastAPI app lives here. No inline SQL in routers or services.
Functions are synchronous (called via asyncio.to_thread inside the session context).
"""

import json
import psycopg
from psycopg.rows import dict_row


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
