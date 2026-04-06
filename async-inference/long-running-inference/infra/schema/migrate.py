"""
Schema migration script for Lakebase Autoscaling.
Generates an OAuth token at runtime — no static password needed.

Usage (via DAB):
    databricks bundle run schema_migration

Reads config from CLI args (injected by DAB spark_python_task.parameters):
    --lakebase_project_id, --lakebase_branch_id, --lakebase_db_name
    --app_name, --mlflow_experiment_name
Falls back to environment variables for local runs.
"""

import argparse
import logging
import os
import pathlib

import psycopg
from psycopg import sql as pgsql
from databricks.sdk import WorkspaceClient

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

# ── Embedded schema SQL (fallback for serverless exec context where __file__ is unavailable)
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS job_requests (
    job_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    payload     JSONB       NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'PENDING'
        CONSTRAINT chk_job_requests_status
            CHECK (status IN ('PENDING', 'RUNNING', 'STREAMING', 'DONE', 'FAILED')),
    claimed_by  TEXT,
    error_msg   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_chunks (
    chunk_id    BIGSERIAL   PRIMARY KEY,
    job_id      UUID        NOT NULL REFERENCES job_requests(job_id) ON DELETE CASCADE,
    chunk_index INT         NOT NULL,
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_job_chunks_job_chunk UNIQUE (job_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS job_results (
    job_id        UUID        PRIMARY KEY REFERENCES job_requests(job_id) ON DELETE CASCADE,
    full_text     TEXT        NOT NULL,
    total_tokens  INT,
    prompt_tokens INT,
    latency_ms    INT,
    completed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_requests_pending
    ON job_requests (updated_at)
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_job_requests_running_streaming
    ON job_requests (status, updated_at)
    WHERE status IN ('RUNNING', 'STREAMING');

CREATE INDEX IF NOT EXISTS idx_job_chunks_job
    ON job_chunks (job_id, chunk_index);
"""

# ── Idempotent migrations applied after the base schema (safe to re-run)
_IDEMPOTENT_MIGRATIONS = [
    # UNIQUE constraint on job_chunks — prevents duplicate chunks on worker retry
    """
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_job_chunks_job_chunk'
        ) THEN
            ALTER TABLE job_chunks
                ADD CONSTRAINT uq_job_chunks_job_chunk UNIQUE (job_id, chunk_index);
        END IF;
    END $$
    """,
    # CHECK constraint on job_requests.status — rejects invalid status values at DB level
    """
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'chk_job_requests_status'
        ) THEN
            ALTER TABLE job_requests
                ADD CONSTRAINT chk_job_requests_status
                CHECK (status IN ('PENDING', 'RUNNING', 'STREAMING', 'DONE', 'FAILED'));
        END IF;
    END $$
    """,
    # Index for stuck-job detection (reconciler RUNNING/STREAMING sweep)
    """
    CREATE INDEX IF NOT EXISTS idx_job_requests_running_streaming
        ON job_requests (status, updated_at)
        WHERE status IN ('RUNNING', 'STREAMING')
    """,
    # Migrate existing PENDING index from created_at to updated_at (more correct for resets)
    """
    DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM pg_indexes WHERE indexname = 'idx_job_requests_pending'
        ) THEN
            DROP INDEX idx_job_requests_pending;
        END IF;
    END $$
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_requests_pending
        ON job_requests (updated_at)
        WHERE status = 'PENDING'
    """,
]


def get_connection(w: WorkspaceClient, project_id: str, branch_id: str, db_name: str):
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
        connect_timeout=30,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    # DAB silently drops task-level environment_variables for serverless jobs.
    # All config is passed as CLI parameters and injected into os.environ here.
    parser.add_argument("--lakebase_project_id", default="")
    parser.add_argument("--lakebase_branch_id", default="")
    parser.add_argument("--lakebase_db_name", default="")
    parser.add_argument("--app_name", default="")
    parser.add_argument("--mlflow_experiment_name", default="")
    return parser.parse_args()


def main():
    args = parse_args()

    # CLI args take precedence; fall back to env vars for local runs.
    project_id = args.lakebase_project_id or os.environ.get("LAKEBASE_PROJECT_ID", "long-running-inference")
    branch_id = args.lakebase_branch_id or os.environ.get("LAKEBASE_BRANCH_ID", "production")
    db_name = args.lakebase_db_name or os.environ.get("LAKEBASE_DB_NAME", "databricks_postgres")
    if args.app_name:
        os.environ["APP_NAME"] = args.app_name
    if args.mlflow_experiment_name:
        os.environ["MLFLOW_EXPERIMENT_NAME"] = args.mlflow_experiment_name

    sql_path_env = os.environ.get("MIGRATION_SQL_PATH")
    if sql_path_env:
        sql = pathlib.Path(sql_path_env).read_text()
    elif "__file__" in dir():
        sql = (pathlib.Path(__file__).parent / "00_init.sql").read_text()
    else:
        # Fallback: SQL embedded inline for Databricks serverless exec context (no __file__)
        sql = _SCHEMA_SQL

    logger.info("Connecting to project=%s branch=%s db=%s", project_id, branch_id, db_name)
    w = WorkspaceClient()

    with get_connection(w, project_id, branch_id, db_name) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for migration in _IDEMPOTENT_MIGRATIONS:
                cur.execute(migration)
        conn.commit()

    logger.info("Schema migration complete")

    # ── Provision Postgres role for the Databricks App's service principal ────
    # Without this, the SP's OAuth token is rejected at the database level even if it
    # has project-level CAN_USE. The databricks_auth extension maps Databricks identities
    # to Postgres roles; databricks_create_role() is idempotent via the DO block below.
    app_name = os.environ.get("APP_NAME")
    client_id = None

    if app_name:
        try:
            app = w.apps.get(app_name)
            # app.service_principal_id is the numeric SCIM id; SP's applicationId (UUID)
            # is what Lakebase uses as the Postgres username / role name.
            sp = w.service_principals.get(id=app.service_principal_id)
            client_id = str(sp.application_id)

            with get_connection(w, project_id, branch_id, db_name) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS databricks_auth")
                    # databricks_create_role maps the Databricks identity → Postgres role.
                    # Wrapped in DO block so re-running the migration doesn't fail.
                    cur.execute(pgsql.SQL("""
                        DO $$ BEGIN
                            PERFORM databricks_create_role({client_id}, 'service_principal');
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END $$
                    """).format(client_id=pgsql.Literal(client_id)))

                    # Use Identifier for role and database names — prevents SQL injection
                    cur.execute(pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        pgsql.Identifier(db_name), pgsql.Identifier(client_id)))
                    cur.execute(pgsql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        pgsql.Identifier(client_id)))
                    cur.execute(pgsql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(pgsql.Identifier(client_id)))
                    cur.execute(pgsql.SQL(
                        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(pgsql.Identifier(client_id)))
                conn.commit()

            logger.info("Created Postgres role and granted table access to app SP %s (%s)",
                        client_id, app_name)
        except Exception as e:
            logger.warning("Could not provision Postgres role for app SP: %s", e)
    else:
        logger.info("APP_NAME not set — skipping app SP Postgres role provisioning")

    # ── Grant app SP CAN_MANAGE on the MLflow experiment ─────────────────────
    # DAB apps.resources doesn't support MLflow experiments natively, so we use the SDK.
    # We use MlflowClient.get_experiment_by_name() with a fallback to create_experiment()
    # so this step is resilient even if the DAB experiment resource hasn't propagated yet.
    mlflow_experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME", "")
    if mlflow_experiment_name and client_id:
        try:
            import mlflow
            from mlflow.tracking import MlflowClient
            mlflow.set_tracking_uri("databricks")
            client = MlflowClient()

            experiment = client.get_experiment_by_name(mlflow_experiment_name)
            if experiment is None:
                # DAB may not have created it yet (or name differed) — create it ourselves.
                exp_id = client.create_experiment(mlflow_experiment_name)
                logger.info("Created MLflow experiment %s (id=%s)", mlflow_experiment_name, exp_id)
            else:
                exp_id = experiment.experiment_id
                logger.info("Found MLflow experiment %s (id=%s)", mlflow_experiment_name, exp_id)

            if exp_id:
                from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
                w.permissions.set(
                    request_object_type="experiments",
                    request_object_id=exp_id,
                    access_control_list=[
                        AccessControlRequest(
                            service_principal_name=client_id,
                            permission_level=PermissionLevel.CAN_MANAGE,
                        )
                    ],
                )
                logger.info("Granted CAN_MANAGE on MLflow experiment %s to app SP %s",
                            mlflow_experiment_name, client_id)
        except Exception as e:
            logger.warning("Could not grant MLflow experiment access to app SP: %s", e)
    elif mlflow_experiment_name and not client_id:
        logger.info("MLflow experiment name set but no app SP found — skipping permission grant")


if __name__ == "__main__":
    main()
