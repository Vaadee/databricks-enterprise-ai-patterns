"""
Single-use Lakebase connection for the worker.

The worker is a short-lived process (exits after one job, well under 1 hour),
so one OAuth token and one connection is all it needs — no pool, no refresh.
"""

import os

import psycopg
from databricks.sdk import WorkspaceClient


def get_conn() -> psycopg.Connection:
    project_id = os.environ["LAKEBASE_PROJECT_ID"]
    branch_id = os.environ.get("LAKEBASE_BRANCH_ID", "production")
    db_name = os.environ.get("LAKEBASE_DB_NAME", "databricks_postgres")

    w = WorkspaceClient()

    ep_parent = f"projects/{project_id}/branches/{branch_id}"
    endpoints = list(w.postgres.list_endpoints(parent=ep_parent))
    if not endpoints:
        raise RuntimeError(f"No endpoints found for {ep_parent}")

    ep_name = endpoints[0].name
    endpoint = w.postgres.get_endpoint(name=ep_name)
    host = endpoint.status.hosts.host

    cred = w.postgres.generate_database_credential(endpoint=ep_name)
    username = w.current_user.me().user_name

    return psycopg.connect(
        host=host,
        dbname=db_name,
        user=username,
        password=cred.token,
        sslmode="require",
        connect_timeout=30,  # fail fast if Lakebase endpoint is unreachable
    )
