"""
Lakeflow Job worker entrypoint.

Receives job_id as a CLI argument (passed by Lakeflow via spark_python_task parameters).
Claims one job, runs streaming inference, writes result to Lakebase, exits.

One Lakeflow run = one document. Concurrency = number of parallel runs (max_concurrent_runs).
"""

import argparse
import logging
import os
import sys
import uuid as _uuid

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

from db.connection import get_conn
from db import queries
from psycopg.rows import dict_row
from services.azure_openai import stream_completion


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job_id", required=True, help="UUID of the job_request to process")
    # DAB environment_variables is silently dropped for serverless spark_python_task.
    # All config is passed as CLI parameters and injected into os.environ here.
    parser.add_argument("--lakebase_project_id", default="")
    parser.add_argument("--lakebase_branch_id", default="production")
    parser.add_argument("--lakebase_db_name", default="databricks_postgres")
    parser.add_argument("--foundation_model_name", default="")
    parser.add_argument("--flush_every", default="1000")
    parser.add_argument("--mlflow_experiment_name", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Validate job_id early — fail fast with a clear error rather than a cryptic DB error
    try:
        _uuid.UUID(args.job_id)
    except ValueError:
        logger.error("Invalid job_id: %r is not a valid UUID", args.job_id)
        sys.exit(1)

    # Inject config into os.environ so helper modules (connection.py, azure_openai.py) read them.
    if args.lakebase_project_id:
        os.environ["LAKEBASE_PROJECT_ID"] = args.lakebase_project_id
    if args.lakebase_branch_id:
        os.environ["LAKEBASE_BRANCH_ID"] = args.lakebase_branch_id
    if args.lakebase_db_name:
        os.environ["LAKEBASE_DB_NAME"] = args.lakebase_db_name
    if args.foundation_model_name:
        os.environ["FOUNDATION_MODEL_NAME"] = args.foundation_model_name
    if args.flush_every:
        os.environ["FLUSH_EVERY"] = args.flush_every
    if args.mlflow_experiment_name:
        os.environ["MLFLOW_EXPERIMENT_NAME"] = args.mlflow_experiment_name

    job_id = args.job_id

    # Use the Lakeflow run_id as the worker identity for claimed_by
    worker_id = os.environ.get("DATABRICKS_JOB_RUN_ID", f"local-{os.getpid()}")

    logger.info("Worker starting: job_id=%s worker_id=%s", job_id, worker_id)

    conn = get_conn()

    try:
        # Claim the specific job_id (reconciler may have re-triggered, check it's still PENDING)
        # We claim by job_id directly rather than any PENDING job, since this worker was
        # triggered for a specific job_id.
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
            row = cur.fetchone()

        if row is None:
            logger.info(
                "job_id=%s not PENDING (already claimed or completed) — exiting safely", job_id
            )
            return

        payload = row["payload"]
        logger.info("job_id=%s claimed by worker_id=%s", job_id, worker_id)

        stream_completion(conn, job_id, payload)

    finally:
        conn.close()

    logger.info("Worker done: job_id=%s", job_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Worker failed")
        sys.exit(1)
