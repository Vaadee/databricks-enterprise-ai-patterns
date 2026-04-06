"""
Triggers a Lakeflow worker job run via the Databricks Jobs SDK.

WorkspaceClient() auto-detects credentials:
- In Databricks Apps: DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET (auto-injected)
- Locally: ~/.databrickscfg DEFAULT profile or DATABRICKS_HOST + DATABRICKS_TOKEN env vars
"""

import logging
import os

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

# Initialize eagerly at module load time so all requests share one client.
# Avoids the race condition where two concurrent requests both see _w is None
# and create duplicate WorkspaceClient instances (double-checked locking without a lock).
# Credentials are always available when Databricks Apps starts.
_w: WorkspaceClient = WorkspaceClient()


def trigger_worker(job_id: str) -> str:
    """
    Fire-and-forget: trigger the worker Lakeflow Job with the given job_id.
    Returns the Lakeflow run_id as a string.
    Raises on failure (caller catches and logs — job is safe in DB, reconciler will retry).
    """
    worker_job_id = int(os.environ["WORKER_JOB_ID"])
    run = _w.jobs.run_now(
        job_id=worker_job_id,
        job_parameters={"job_id": job_id},
    )
    logger.info("Triggered worker run_id=%s for job_id=%s", run.run_id, job_id)
    return str(run.run_id)
