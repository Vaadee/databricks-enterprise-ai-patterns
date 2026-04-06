"""
Reconciler — scheduled every 5 minutes as a serverless Lakeflow Job.

Three recovery actions per run:
  1. PENDING  jobs stuck > stale_pending_minutes  → re-trigger the worker
  2. RUNNING  jobs stuck > stale_running_minutes  → reset to PENDING + delete partial
     chunks, then re-trigger (worker crashed before mark_streaming)
  3. STREAMING jobs stuck > stale_streaming_minutes → mark FAILED (can't cleanly
     restart mid-stream; partial chunks are inconsistent)

Idempotent: safe to run multiple times. The worker claims jobs with
SELECT FOR UPDATE SKIP LOCKED, so duplicate triggers are harmless.
"""

import argparse
import logging
import os
import sys

import psycopg
from psycopg.rows import dict_row
from databricks.sdk import WorkspaceClient

# Allow running from repo root.
# __file__ is not defined when exec'd in Databricks serverless; fall back to the
# frame's co_filename, which exec(compile(src, filename, 'exec')) sets correctly.
_this_file = __file__ if "__file__" in dir() else sys._getframe(0).f_code.co_filename
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(_this_file))))

from worker.db.connection import get_conn

# ── Colored logging setup ─────────────────────────────────────────────────────
try:
    import colorlog
    _handler = colorlog.StreamHandler()
    _handler.setFormatter(colorlog.ColoredFormatter(
        fmt="%(log_color)s%(asctime)s %(levelname)s %(name)s%(reset)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    logging.getLogger().addHandler(_handler)
    logging.getLogger().setLevel(logging.INFO)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

logger = logging.getLogger(__name__)


# ── Query functions ───────────────────────────────────────────────────────────

def get_stale_pending_jobs(conn: psycopg.Connection, minutes: int) -> list[str]:
    """Return job_ids with status=PENDING that haven't been updated in > minutes."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT job_id::text
            FROM job_requests
            WHERE status = 'PENDING'
              AND updated_at < now() - (interval '1 minute' * %s)
            ORDER BY updated_at
            """,
            (minutes,),
        )
        return [row["job_id"] for row in cur.fetchall()]


def reset_stale_running_jobs(conn: psycopg.Connection, minutes: int) -> list[str]:
    """
    Reset RUNNING jobs stuck > minutes back to PENDING and delete their partial chunks.
    These are jobs where the worker claimed the job but crashed before mark_streaming().
    Atomic: UPDATE and DELETE are committed together.
    Returns the list of reset job_ids so they can be immediately re-triggered.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'PENDING',
                claimed_by = NULL,
                updated_at = now()
            WHERE status = 'RUNNING'
              AND updated_at < now() - (interval '1 minute' * %s)
            RETURNING job_id::text
            """,
            (minutes,),
        )
        job_ids = [row["job_id"] for row in cur.fetchall()]
        if job_ids:
            # Purge any partial chunks so the retry starts from a clean slate
            cur.execute(
                "DELETE FROM job_chunks WHERE job_id = ANY(%s::uuid[])",
                (job_ids,),
            )
    conn.commit()
    return job_ids


def fail_stale_streaming_jobs(conn: psycopg.Connection, minutes: int) -> list[str]:
    """
    Mark STREAMING jobs stuck > minutes as FAILED.
    Cannot cleanly restart a mid-stream job: partial chunks written so far are
    inconsistent, and the UNIQUE(job_id, chunk_index) constraint would reject a
    clean retry anyway. Marking FAILED lets clients surface the error and retry.
    Returns the list of job_ids that were marked FAILED.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE job_requests
            SET status = 'FAILED',
                error_msg = 'Worker lost during streaming — timed out after '
                            || %s || ' minutes without progress',
                updated_at = now()
            WHERE status = 'STREAMING'
              AND updated_at < now() - (interval '1 minute' * %s)
            RETURNING job_id::text
            """,
            (minutes, minutes),
        )
        job_ids = [row["job_id"] for row in cur.fetchall()]
    conn.commit()
    return job_ids


def trigger_worker(job_id: str, w: WorkspaceClient) -> None:
    worker_job_id = int(os.environ["WORKER_JOB_ID"])
    run = w.jobs.run_now(
        job_id=worker_job_id,
        job_parameters={"job_id": job_id},
    )
    logger.info("Re-triggered job_id=%s → run_id=%s", job_id, run.run_id)


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    # DAB silently drops task-level environment_variables for serverless jobs.
    # All config is passed as CLI parameters and injected into os.environ here.
    parser.add_argument("--lakebase_project_id", default="")
    parser.add_argument("--lakebase_branch_id", default="production")
    parser.add_argument("--lakebase_db_name", default="databricks_postgres")
    parser.add_argument("--worker_job_id", default="")
    # Stale-job thresholds (configurable via DAB variables)
    parser.add_argument("--stale_pending_minutes",   type=int, default=3,
                        help="Minutes before a PENDING job is re-triggered")
    parser.add_argument("--stale_running_minutes",   type=int, default=5,
                        help="Minutes before a RUNNING job is reset to PENDING")
    parser.add_argument("--stale_streaming_minutes", type=int, default=30,
                        help="Minutes before a STREAMING job is marked FAILED")
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Inject config into os.environ so helper modules (worker.db.connection) read them.
    if args.lakebase_project_id:
        os.environ["LAKEBASE_PROJECT_ID"] = args.lakebase_project_id
    if args.lakebase_branch_id:
        os.environ["LAKEBASE_BRANCH_ID"] = args.lakebase_branch_id
    if args.lakebase_db_name:
        os.environ["LAKEBASE_DB_NAME"] = args.lakebase_db_name
    if args.worker_job_id:
        os.environ["WORKER_JOB_ID"] = args.worker_job_id

    logger.info(
        "Reconciler starting — thresholds: pending=%dm running=%dm streaming=%dm",
        args.stale_pending_minutes,
        args.stale_running_minutes,
        args.stale_streaming_minutes,
    )

    conn = get_conn()

    try:
        # 1. Recover stuck RUNNING jobs → reset to PENDING + delete partial chunks
        reset_running = reset_stale_running_jobs(conn, args.stale_running_minutes)
        if reset_running:
            logger.warning(
                "Reset %d stale RUNNING job(s) → PENDING: %s",
                len(reset_running), reset_running,
            )

        # 2. Fail stuck STREAMING jobs — can't restart mid-stream
        failed_streaming = fail_stale_streaming_jobs(conn, args.stale_streaming_minutes)
        if failed_streaming:
            logger.warning(
                "Marked %d stale STREAMING job(s) → FAILED: %s",
                len(failed_streaming), failed_streaming,
            )

        # 3. Find stale PENDING jobs + just-reset RUNNING jobs to trigger
        stale_pending = get_stale_pending_jobs(conn, args.stale_pending_minutes)
        # Deduplicate: reset_running jobs are now PENDING but may not be > stale_pending_minutes
        # since we just updated their updated_at; include them explicitly.
        to_trigger = list(dict.fromkeys(stale_pending + reset_running))

        logger.info(
            "Found %d stale PENDING + %d reset-from-RUNNING = %d total to trigger",
            len(stale_pending), len(reset_running), len(to_trigger),
        )

        if not to_trigger:
            logger.info("Reconciler done: nothing to do")
            return

        w = WorkspaceClient()
        triggered = 0
        for job_id in to_trigger:
            try:
                trigger_worker(job_id, w)
                triggered += 1
            except Exception:
                logger.exception("Failed to re-trigger job_id=%s", job_id)

        logger.info("Reconciler done: triggered %d / %d jobs", triggered, len(to_trigger))

    finally:
        conn.close()


if __name__ == "__main__":
    main()
