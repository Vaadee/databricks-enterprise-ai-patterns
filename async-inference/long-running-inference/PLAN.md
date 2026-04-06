# long-running-inference — implementation plan
> Databricks App (FastAPI) + Lakebase Autoscaling + Lakeflow Jobs (serverless)
> Target: give this to Claude Code and get a working system
>
> **Repo:** `databricks-enterprise-ai-patterns`
> **Path:** `async-inference/long-running-inference/`
> **Start here:** `cd databricks-enterprise-ai-patterns/async-inference/long-running-inference`

---

## what we're building

An async long-running inference system on Databricks that:
- Accepts large documents via REST API
- Returns a job_id immediately (no waiting)
- Processes the document using Azure OpenAI GPT (streaming, 15+ min safe)
- Stores results progressively in Lakebase Autoscaling (Postgres)
- Exposes status/result polling endpoints
- Is fully deployable via Databricks Asset Bundles (DAB)

No model serving endpoint. One Databricks App does everything the client touches.
Workers and reconciler run as serverless Lakeflow Jobs — no cluster management.

---

## repository layout

```
databricks-enterprise-ai-patterns/
└── async-inference/
    └── long-running-inference/      ← you are here
        │
        ├── databricks.yml                  # DAB root config
        ├── .env.example                    # env var template (never commit .env)
        ├── .gitignore
        ├── README.md
        │
        ├── app/                            # Databricks App (FastAPI gateway)
        │   ├── app.py                      # FastAPI entrypoint
        │   ├── app.yaml                    # App runtime config (uvicorn command + env)
        │   ├── routers/
        │   │   ├── __init__.py
        │   │   ├── jobs.py                 # /submit, /status/{id}, /result/{id}
        │   │   └── health.py               # /health, /ready
        │   ├── db/
        │   │   ├── __init__.py
        │   │   ├── connection.py           # OAuth token pool + refresh + get_conn()
        │   │   └── queries.py              # all SQL in one place, no inline SQL elsewhere
        │   ├── services/
        │   │   ├── __init__.py
        │   │   └── job_trigger.py          # Databricks Jobs API trigger call
        │   ├── models/
        │   │   ├── __init__.py
        │   │   └── schemas.py              # Pydantic request/response models
        │   └── requirements.txt
        │
        ├── worker/                         # Lakeflow Job (inference worker, serverless)
        │   ├── worker.py                   # entrypoint — claim job, stream, write result
        │   ├── db/
        │   │   ├── __init__.py
        │   │   ├── connection.py           # OAuth token connection (single-use, no pool)
        │   │   └── queries.py
        │   ├── services/
        │   │   ├── __init__.py
        │   │   └── azure_openai.py         # streaming inference logic
        │   └── requirements.txt
        │
        ├── infra/
        │   ├── schema/
        │   │   ├── 00_init.sql             # full Lakebase schema — run once
        │   │   └── migrate.py              # runs schema via OAuth token (no static password)
        │   └── reconciler/
        │       └── reconciler.py           # scheduled job: re-trigger stuck PENDING rows
        │
        ├── resources/                      # DAB resource definitions (YAML)
        │   ├── app.yml                     # Databricks App definition
        │   ├── worker_job.yml              # Lakeflow Job definition (inference worker)
        │   └── reconciler_job.yml          # Lakeflow Job definition (reconciler)
        │
        └── tests/
            ├── unit/
            │   ├── test_schemas.py
            │   └── test_queries.py
            ├── integration/
            │   └── test_api.py             # end-to-end submit → poll → result
            └── conftest.py
```

---

## lakebase schema — `infra/schema/00_init.sql`

Three tables. Run once via `infra/schema/migrate.py` (uses OAuth token — no static password).

```sql
-- job_requests: one row per submitted document
CREATE TABLE IF NOT EXISTS job_requests (
    job_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    payload      JSONB        NOT NULL,
    -- payload shape: {messages: [...], max_tokens: int, caller_id: str}
    status       TEXT         NOT NULL DEFAULT 'PENDING',
    -- state machine: PENDING → RUNNING → STREAMING → DONE | FAILED
    claimed_by   TEXT,                        -- lakeflow run_id that owns this job
    error_msg    TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- partial results written as tokens stream in
CREATE TABLE IF NOT EXISTS job_chunks (
    chunk_id     BIGSERIAL    PRIMARY KEY,
    job_id       UUID         NOT NULL REFERENCES job_requests(job_id) ON DELETE CASCADE,
    chunk_index  INT          NOT NULL,
    content      TEXT         NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- final assembled result written once on completion
CREATE TABLE IF NOT EXISTS job_results (
    job_id        UUID         PRIMARY KEY REFERENCES job_requests(job_id) ON DELETE CASCADE,
    full_text     TEXT         NOT NULL,
    total_tokens  INT,
    prompt_tokens INT,
    latency_ms    INT,
    completed_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- indexes
CREATE INDEX IF NOT EXISTS idx_job_requests_pending
    ON job_requests (created_at)
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_job_chunks_job
    ON job_chunks (job_id, chunk_index);
```

**state machine rules (enforce in code, not DB constraints):**
```
PENDING   → RUNNING    (worker claims job via SELECT FOR UPDATE SKIP LOCKED)
RUNNING   → STREAMING  (worker marks streaming after first token received)
STREAMING → DONE       (worker writes full result, updates status)
STREAMING → FAILED     (worker catches exception mid-stream)
RUNNING   → FAILED     (worker fails before first token)
PENDING   → PENDING    (reconciler leaves alone if < 3 min old)
PENDING   → re-trigger (reconciler fires if > 3 min old with no active run)
```

---

## schema migration script — `infra/schema/migrate.py`

```python
# Generates a fresh OAuth token, connects to Lakebase Autoscaling, runs 00_init.sql.
# Run once before first deploy:
#   python infra/schema/migrate.py
#
# Reads from env:
#   LAKEBASE_PROJECT_ID   — Lakebase Autoscaling project ID
#   LAKEBASE_BRANCH_ID    — branch (default: production)
#   LAKEBASE_DB_NAME      — database name (default: databricks_postgres)
#
# Uses WorkspaceClient() — auto-detects auth from ~/.databrickscfg or env vars
```

---

## pydantic schemas — `app/models/schemas.py`

```python
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID
from datetime import datetime

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)

class SubmitRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1)
    max_tokens: int = Field(default=4096, ge=1, le=16384)
    caller_id: str | None = None  # optional audit field

class SubmitResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: datetime

class StatusResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None

class ResultResponse(BaseModel):
    job_id: UUID
    status: str
    result: str | None = None        # full text if DONE
    partial: str | None = None       # assembled chunks if STREAMING
    total_tokens: int | None = None
    latency_ms: int | None = None
    note: str | None = None
```

---

## app entrypoint — `app/app.py`

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.db.connection import db_manager
from app.routers import jobs, health

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_manager.initialize()   # connect to Lakebase, start token refresh loop
    yield
    await db_manager.close()        # cancel refresh task, dispose pool

app = FastAPI(
    title="long-running-inference",
    description="Async long-running document inference via Azure OpenAI",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(jobs.router, prefix="/jobs")
```

---

## app runtime config — `app/app.yaml`

Databricks Apps runtime config. This is NOT the DAB resource file — it lives inside `app/`
alongside the source code and controls how the app process is started.

```yaml
command:
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"

env:
  - name: LAKEBASE_PROJECT_ID
    valueFrom: lakebase-project-id       # injected from app resource
  - name: WORKER_JOB_ID
    value: "${resources.jobs.long_running_inference_worker.id}"
  - name: FLUSH_EVERY
    value: "1000"
```

> Note: `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` are auto-injected by
> Databricks Apps — never set them manually. `WorkspaceClient()` picks them up automatically
> for both job triggering and Lakebase OAuth token generation.

---

## routers — `app/routers/jobs.py`

```python
from fastapi import APIRouter, HTTPException, Depends
from app.models.schemas import SubmitRequest, SubmitResponse, StatusResponse, ResultResponse
from app.db.connection import db_manager
from app.db import queries
from app.services.job_trigger import trigger_worker
from uuid import UUID

router = APIRouter(tags=["jobs"])

@router.post("/submit", response_model=SubmitResponse, status_code=202)
async def submit(body: SubmitRequest):
    async with db_manager.session() as conn:
        row = await queries.insert_job(conn, body.model_dump())
    # best-effort trigger — reconciler handles failures
    try:
        trigger_worker(str(row["job_id"]))
    except Exception:
        pass  # job is safely in DB; reconciler will re-trigger
    return SubmitResponse(**row)

@router.get("/status/{job_id}", response_model=StatusResponse)
async def status(job_id: UUID):
    async with db_manager.session() as conn:
        row = await queries.get_job_status(conn, str(job_id))
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return StatusResponse(**row)

@router.get("/result/{job_id}", response_model=ResultResponse)
async def result(job_id: UUID):
    async with db_manager.session() as conn:
        row = await queries.get_job_result(conn, str(job_id))
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return ResultResponse(**row)
```

---

## db connection — `app/db/connection.py`

Lakebase Autoscaling uses short-lived OAuth tokens (1-hour expiry). The app runs a background
refresh task that replaces the token every 50 minutes before it expires.

```python
# LakebaseAutoscaleConnectionManager:
#   - On initialize():
#       1. Use WorkspaceClient() to find endpoint host (w.postgres.list_endpoints)
#       2. Generate initial OAuth token (w.postgres.generate_database_credential)
#       3. Build async SQLAlchemy engine with psycopg3 driver
#       4. Inject token on each connection via SQLAlchemy event hook
#       5. Start background asyncio task to refresh token every 50 minutes
#   - session() context manager yields an async connection
#   - On close(): cancel refresh task, dispose engine
#
# Key points:
#   - Token injected at connect-time via engine event, not in URL (avoids stale token)
#   - pool_recycle=3600 to respect Lakebase's 24h idle / 3-day max lifetime limits
#   - Always sslmode=require
#   - WorkspaceClient() auto-uses DATABRICKS_CLIENT_ID/SECRET injected by Databricks Apps
#
# Reads from env:
#   LAKEBASE_PROJECT_ID   — project ID (e.g. "long-running-inference")
#   LAKEBASE_BRANCH_ID    — branch (default: "production")
#   LAKEBASE_DB_NAME      — database name (default: "databricks_postgres")
```

---

## db queries — `app/db/queries.py` and `worker/db/queries.py`

All SQL lives here. No inline SQL anywhere else in the codebase.
App queries are async (psycopg3 async); worker queries are sync (psycopg3 sync, simpler).

```python
# --- app/db/queries.py (async) ---

async def insert_job(conn, payload: dict) -> dict:
    # INSERT INTO job_requests (payload) VALUES (%s) RETURNING *

async def get_job_status(conn, job_id: str) -> dict | None:
    # SELECT job_id, status, created_at, updated_at, error_msg
    # FROM job_requests WHERE job_id = %s::uuid

async def get_job_result(conn, job_id: str) -> dict | None:
    # check status first
    # if DONE: SELECT from job_results JOIN job_requests
    # if STREAMING: SELECT content FROM job_chunks ORDER BY chunk_index → assemble
    # else: return status only with note

# --- worker/db/queries.py (sync) ---

def claim_job(conn, worker_id: str) -> dict | None:
    # UPDATE job_requests SET status='RUNNING', claimed_by=%s, updated_at=now()
    # WHERE job_id = (
    #   SELECT job_id FROM job_requests
    #   WHERE status='PENDING'
    #   ORDER BY created_at
    #   FOR UPDATE SKIP LOCKED LIMIT 1
    # ) RETURNING job_id, payload

def mark_streaming(conn, job_id: str) -> None:
    # UPDATE job_requests SET status='STREAMING', updated_at=now()

def write_chunk(conn, job_id: str, index: int, content: str) -> None:
    # INSERT INTO job_chunks (job_id, chunk_index, content)

def mark_done(conn, job_id: str, full_text: str, tokens: dict, latency_ms: int) -> None:
    # INSERT INTO job_results (...)
    # UPDATE job_requests SET status='DONE', updated_at=now()
    # both in one transaction

def mark_failed(conn, job_id: str, error: str) -> None:
    # UPDATE job_requests SET status='FAILED', error_msg=%s, updated_at=now()
```

---

## worker db connection — `worker/db/connection.py`

The worker is a short-lived process (exits after one job, well under 1 hour).
No pool or refresh needed — one OAuth token, one connection, done.

```python
# get_conn(project_id, branch_id, db_name) -> psycopg.Connection:
#   1. WorkspaceClient() — auto-detects auth in serverless env
#   2. w.postgres.list_endpoints → get primary endpoint name + host
#   3. w.postgres.generate_database_credential(endpoint=ep_name) → token
#   4. psycopg.connect(host=host, dbname=db_name, user=username,
#                      password=token, sslmode="require")
#   5. Return connection (caller closes it)
#
# Reads from env:
#   LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH_ID, LAKEBASE_DB_NAME
```

---

## worker entrypoint — `worker/worker.py`

```python
# Worker receives job_id as a Lakeflow Job parameter.
# In spark_python_task, job parameters are passed as CLI args:
#   --job_id {{job.parameters.job_id}}
#
# flow:
#   1. parse --job_id from sys.argv
#   2. connect to Lakebase (OAuth token via SDK)
#   3. claim the job (SELECT FOR UPDATE SKIP LOCKED)
#      - if already claimed: exit 0 (reconciler race, safe)
#   4. call azure_openai.stream_completion(conn, job_id, payload)
#   5. write chunks as they arrive
#   6. mark DONE or FAILED
#   7. exit 0 (Lakeflow marks run as Succeeded)
#
# important: worker exits after ONE job
# concurrency = number of parallel Lakeflow Job runs, not threads
# no Spark, no cluster needed → runs on serverless compute
```

---

## azure openai service — `worker/services/azure_openai.py`

```python
# AzureOpenAI client configured from env vars:
#   AZURE_OPENAI_ENDPOINT
#   AZURE_OPENAI_KEY
#   AZURE_DEPLOYMENT_NAME
#   AZURE_API_VERSION (default: "2024-02-01")
#
# stream_completion(conn, job_id, payload):
#   - timeout=1800 on client (30 min ceiling, well above 15 min jobs)
#   - stream=True, stream_options={"include_usage": True}
#   - mark_streaming() after first token received
#   - buffer tokens until FLUSH_EVERY chars (default: 1000 from env)
#   - flush buffer → write_chunk()
#   - on StopIteration: flush remainder, mark_done()
#   - on any Exception: mark_failed(), re-raise for Lakeflow logging
```

---

## job trigger service — `app/services/job_trigger.py`

```python
# trigger_worker(job_id: str) -> str:
#   calls Databricks Jobs API via SDK
#   returns run_id (str)
#
# from databricks.sdk import WorkspaceClient
# w = WorkspaceClient()   # auto-uses DATABRICKS_CLIENT_ID/SECRET from Databricks Apps
#
# WORKER_JOB_ID read from env (set by DAB at deploy time via resource reference)
#
# run = w.jobs.run_now(
#     job_id=int(os.environ["WORKER_JOB_ID"]),
#     job_parameters={"job_id": job_id}
# )
# return str(run.run_id)
```

---

## reconciler — `infra/reconciler/reconciler.py`

```python
# runs on a schedule (every 5 min via serverless Lakeflow Job)
# finds PENDING jobs older than 3 minutes
# re-triggers worker for each one
#
# flow:
#   1. connect to Lakebase (OAuth token — same pattern as worker/db/connection.py)
#   2. SELECT job_id FROM job_requests
#      WHERE status = 'PENDING'
#      AND created_at < now() - interval '3 minutes'
#   3. for each: trigger_worker(job_id)   # same job_trigger logic
#   4. log count of re-triggered jobs
#   5. exit 0
#
# idempotent — safe to run multiple times
# reads LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH_ID, LAKEBASE_DB_NAME, WORKER_JOB_ID from env
```

---

## configuration philosophy

**Everything configurable at `databricks.yml` root — zero code changes to switch clients or envs.**

All names, secrets scope, Lakebase coordinates, tuning knobs, and notification targets are
DAB variables. Client/env-specific overrides go in the `targets:` block only.
Resource YAMLs and code reference `${var.xxx}` exclusively — no hardcoded strings anywhere.

The variables flow into jobs and the app as environment variables, so code reads them from
`os.environ` and never knows which client it's running for.

---

## databricks asset bundles — `databricks.yml`

This is the single place a new client/env deployment needs to touch. Override variables
in the target block; everything else inherits the defaults.

```yaml
bundle:
  name: long-running-inference   # bundle identity — keep fixed

include:
  - resources/*.yml

# ── All configurable values live here ────────────────────────────────────────
variables:

  # Identity — drives all resource names
  app_name:
    description: "Base name for all resources. Change per client/deployment."
    default: "long-running-inference"

  # Secret scope — where all secrets are stored
  secret_scope:
    description: "Databricks secret scope name."
    default: "long-running-inference"

  # Service principal for prod run_as
  service_principal:
    description: "Service principal name for prod deployments."
    default: "long-running-inference-sp"

  # Lakebase Autoscaling
  lakebase_project_id:
    description: "Lakebase Autoscaling project ID."
    default: "long-running-inference"
  lakebase_branch_id:
    description: "Lakebase branch (default: production)."
    default: "production"
  lakebase_db_name:
    description: "Postgres database name inside the project."
    default: "databricks_postgres"

  # Worker tuning
  worker_max_concurrent_runs:
    description: "Max parallel worker job runs."
    default: "50"

  # Reconciler schedule
  reconciler_cron:
    description: "Quartz cron for the reconciler job."
    default: "0 */5 * * * ?"   # every 5 minutes

  # Tuning — all readable by code via env vars
  flush_every:
    description: "Chars buffered before flushing a chunk to Lakebase."
    default: "1000"
  token_refresh_secs:
    description: "Seconds between OAuth token refreshes in the app (keep < 3600)."
    default: "3000"
  db_pool_size:
    description: "SQLAlchemy connection pool size (app only)."
    default: "5"
  db_pool_max_overflow:
    description: "SQLAlchemy pool max overflow (app only)."
    default: "10"

  # Azure OpenAI
  azure_api_version:
    description: "Azure OpenAI API version."
    default: "2024-02-01"

  # Notifications
  notification_email:
    description: "Email address for job failure alerts."
    default: "your-team@company.com"

# ── Targets — only override what differs ─────────────────────────────────────
targets:
  dev:
    mode: development
    default: true
    workspace:
      host: ${workspace.host}

  prod:
    mode: production
    workspace:
      host: ${workspace.host}
    run_as:
      service_principal_name: ${var.service_principal}

# Example: client workspace override (add as a new target or override in dev/prod)
#
#   acme-prod:
#     mode: production
#     workspace:
#       host: https://acme.azuredatabricks.net
#     variables:
#       app_name: acme-inference
#       secret_scope: acme-inference
#       lakebase_project_id: acme-inference
#       notification_email: ops@acme.com
#       worker_max_concurrent_runs: "100"
```

---

## `resources/app.yml`

DAB resource definition for the Databricks App. Runtime config (command, env) lives in
`app/app.yaml` — not here. This file tells DAB where the source code lives and injects
all configurable values as environment variables.

```yaml
resources:
  apps:
    long_running_inference_app:
      name: ${var.app_name}-${bundle.target}
      description: "Async long-running inference gateway"
      source_code_path: ../app

      # All env vars injected here — app/app.yaml never hardcodes values
      environment:
        # Lakebase coordinates
        - name: LAKEBASE_PROJECT_ID
          value: ${var.lakebase_project_id}
        - name: LAKEBASE_BRANCH_ID
          value: ${var.lakebase_branch_id}
        - name: LAKEBASE_DB_NAME
          value: ${var.lakebase_db_name}

        # Worker job — resolved at deploy time by DAB
        - name: WORKER_JOB_ID
          value: ${resources.jobs.long_running_inference_worker.id}

        # Azure OpenAI (secret refs use the variable scope name)
        - name: AZURE_OPENAI_ENDPOINT
          value: "{{secrets/${var.secret_scope}/azure-openai-endpoint}}"
        - name: AZURE_OPENAI_KEY
          value: "{{secrets/${var.secret_scope}/azure-openai-key}}"
        - name: AZURE_DEPLOYMENT_NAME
          value: "{{secrets/${var.secret_scope}/azure-deployment-name}}"
        - name: AZURE_API_VERSION
          value: ${var.azure_api_version}

        # Tuning
        - name: FLUSH_EVERY
          value: ${var.flush_every}
        - name: TOKEN_REFRESH_SECS
          value: ${var.token_refresh_secs}
        - name: DB_POOL_SIZE
          value: ${var.db_pool_size}
        - name: DB_POOL_MAX_OVERFLOW
          value: ${var.db_pool_max_overflow}
```

---

## `app/app.yaml`

App runtime config — command only. All env vars come from `resources/app.yml` injection
above; nothing is hardcoded here.

```yaml
command:
  - "uvicorn"
  - "app:app"
  - "--host"
  - "0.0.0.0"
  - "--port"
  - "8000"
```

---

## `resources/worker_job.yml`

Serverless compute — omit `new_cluster` entirely. All configurable values come from
DAB variables injected as task environment variables.

```yaml
resources:
  jobs:
    long_running_inference_worker:
      name: "[${bundle.target}] ${var.app_name}-worker"
      description: "Inference worker — triggered per job submission"

      max_concurrent_runs: ${var.worker_max_concurrent_runs}

      tasks:
        - task_key: run_worker
          spark_python_task:
            python_file: ../worker/worker.py
            parameters:
              - "--job_id"
              - "{{job.parameters.job_id}}"
          environment_key: worker_env
          # no new_cluster → serverless compute

      environments:
        - environment_key: worker_env
          spec:
            client: "1"
            dependencies:
              - "psycopg[binary]>=3.0"
              - openai
              - "databricks-sdk>=0.81.0"

      # All env vars injected from variables — no hardcoded values in code
      tasks:
        - task_key: run_worker
          spark_python_task:
            python_file: ../worker/worker.py
            parameters:
              - "--job_id"
              - "{{job.parameters.job_id}}"
          environment_key: worker_env
          task_env:
            - name: LAKEBASE_PROJECT_ID
              value: ${var.lakebase_project_id}
            - name: LAKEBASE_BRANCH_ID
              value: ${var.lakebase_branch_id}
            - name: LAKEBASE_DB_NAME
              value: ${var.lakebase_db_name}
            - name: AZURE_OPENAI_ENDPOINT
              value: "{{secrets/${var.secret_scope}/azure-openai-endpoint}}"
            - name: AZURE_OPENAI_KEY
              value: "{{secrets/${var.secret_scope}/azure-openai-key}}"
            - name: AZURE_DEPLOYMENT_NAME
              value: "{{secrets/${var.secret_scope}/azure-deployment-name}}"
            - name: AZURE_API_VERSION
              value: ${var.azure_api_version}
            - name: FLUSH_EVERY
              value: ${var.flush_every}

      parameters:
        - name: job_id
          default: ""

      health:
        rules:
          - metric: RUN_DURATION_SECONDS
            op: GREATER_THAN
            value: 3600

      email_notifications:
        on_failure:
          - ${var.notification_email}
```

---

## `resources/reconciler_job.yml`

Also serverless. Inherits same variable-driven env injection pattern.

```yaml
resources:
  jobs:
    long_running_inference_reconciler:
      name: "[${bundle.target}] ${var.app_name}-reconciler"

      schedule:
        quartz_cron_expression: ${var.reconciler_cron}
        timezone_id: "UTC"
        pause_status: UNPAUSED

      tasks:
        - task_key: reconcile
          spark_python_task:
            python_file: ../infra/reconciler/reconciler.py
          environment_key: reconciler_env
          task_env:
            - name: LAKEBASE_PROJECT_ID
              value: ${var.lakebase_project_id}
            - name: LAKEBASE_BRANCH_ID
              value: ${var.lakebase_branch_id}
            - name: LAKEBASE_DB_NAME
              value: ${var.lakebase_db_name}
            - name: WORKER_JOB_ID
              value: ${resources.jobs.long_running_inference_worker.id}
            # reconciler reuses same secret scope
            - name: AZURE_OPENAI_ENDPOINT
              value: "{{secrets/${var.secret_scope}/azure-openai-endpoint}}"

      environments:
        - environment_key: reconciler_env
          spec:
            client: "1"
            dependencies:
              - "psycopg[binary]>=3.0"
              - "databricks-sdk>=0.81.0"

      email_notifications:
        on_failure:
          - ${var.notification_email}
```

---

## requirements

### `app/requirements.txt`

```
psycopg[binary]>=3.0
sqlalchemy>=2.0
databricks-sdk>=0.81.0
```

FastAPI and uvicorn are pre-installed in the Databricks Apps runtime — do NOT add them.

### `worker/requirements.txt`

```
psycopg[binary]>=3.0
openai
databricks-sdk>=0.81.0
```

---

## environment variables — complete list

Code reads ALL values from `os.environ`. Values are injected by DAB at deploy time from
`databricks.yml` variables — no hardcoding anywhere in code or resource YAMLs.

### Injected automatically — never set manually

```
DATABRICKS_CLIENT_ID       → injected by Databricks Apps runtime (app)
DATABRICKS_CLIENT_SECRET   → injected by Databricks Apps runtime (app)
                             worker/reconciler: auto-auth via serverless job context
```

### Set in Databricks secret scope (scope name = `${var.secret_scope}`)

| Secret key | Description |
|---|---|
| `azure-openai-endpoint` | `https://<resource>.openai.azure.com/` |
| `azure-openai-key` | Azure OpenAI API key |
| `azure-deployment-name` | GPT-4 deployment name |

> Lakebase and WORKER_JOB_ID are NOT secrets — they are plain DAB variable values
> injected as env vars. Only credentials that must be redacted go in the secret scope.

### Set as DAB variables in `databricks.yml` (not secrets)

| Variable | Env var injected | Default |
|---|---|---|
| `lakebase_project_id` | `LAKEBASE_PROJECT_ID` | `long-running-inference` |
| `lakebase_branch_id` | `LAKEBASE_BRANCH_ID` | `production` |
| `lakebase_db_name` | `LAKEBASE_DB_NAME` | `databricks_postgres` |
| `azure_api_version` | `AZURE_API_VERSION` | `2024-02-01` |
| `flush_every` | `FLUSH_EVERY` | `1000` |
| `token_refresh_secs` | `TOKEN_REFRESH_SECS` | `3000` |
| `db_pool_size` | `DB_POOL_SIZE` | `5` |
| `db_pool_max_overflow` | `DB_POOL_MAX_OVERFLOW` | `10` |
| `worker_max_concurrent_runs` | (DAB YAML only, not env) | `50` |
| `reconciler_cron` | (DAB YAML only, not env) | `0 */5 * * * ?` |
| `notification_email` | (DAB YAML only, not env) | `your-team@company.com` |

`WORKER_JOB_ID` is resolved by DAB at deploy time via resource reference — not a variable.

---

## deployment sequence

Run all commands from inside `databricks-enterprise-ai-patterns/async-inference/long-running-inference/`.
Replace `<scope>` with your `secret_scope` variable value (default: `long-running-inference`).
Replace `<project-id>` with your `lakebase_project_id` variable value.

```
1. create Lakebase Autoscaling project (one-time per client workspace)
   databricks postgres create-project \
     --project-id <project-id> \
     --json '{"spec": {"display_name": "<project-id>", "pg_version": "17"}}'
   # wait for operation to complete (~1-2 min)

2. create secret scope
   databricks secrets create-scope <scope>

3. populate secrets (only credentials go in scope — everything else is a DAB variable)
   databricks secrets put-secret <scope> azure-openai-endpoint --string-value "https://..."
   databricks secrets put-secret <scope> azure-openai-key      --string-value "..."
   databricks secrets put-secret <scope> azure-deployment-name --string-value "..."

4. run schema migration (uses OAuth token — no static password needed)
   LAKEBASE_PROJECT_ID=<project-id> python infra/schema/migrate.py

5. deploy bundle — override variables for this client if needed
   # default (uses all databricks.yml defaults):
   databricks bundle deploy --target dev

   # custom client workspace (override at CLI — no code changes):
   databricks bundle deploy --target dev \
     --var="app_name=acme-inference" \
     --var="secret_scope=acme-inference" \
     --var="lakebase_project_id=acme-inference" \
     --var="notification_email=ops@acme.com"

6. start the app
   databricks bundle run long_running_inference_app --target dev

7. run integration test
   python tests/integration/test_api.py
```

---

## testing strategy

**Unit tests** (`tests/unit/`):
- `test_schemas.py` — pydantic validation, field constraints, invalid payloads
- `test_queries.py` — SQL query functions with a mock async connection

**Integration tests** (`tests/integration/test_api.py`):
- submit a real job (small doc, real Azure OpenAI call)
- poll status until STREAMING appears
- poll until DONE
- assert result is non-empty string
- assert total_tokens > 0
- assert latency_ms is reasonable
- test 404 on unknown job_id
- test malformed payload returns 422

**Load test** (manual, not automated):
- submit 10 jobs in parallel
- verify all reach DONE within expected window
- verify no double-claiming (each job_id appears in exactly one worker run)

---

## software practices checklist for Claude Code

### configuration & portability
- [ ] `databricks.yml` has a `variables:` block covering every name, scope, coord, and tuning knob
- [ ] every resource name uses `${var.app_name}` — zero literal "long-running-inference" strings in resource YAMLs
- [ ] every secret reference uses `${var.secret_scope}` — not a hardcoded scope name
- [ ] all tuning values (flush_every, pool sizes, token_refresh_secs, cron) are DAB variables with defaults
- [ ] all env vars injected by DAB into app/jobs — code only reads `os.environ`, never has defaults baked in
- [ ] `app/app.yaml` contains only the uvicorn command — no env vars or hardcoded values
- [ ] switching client workspace = adding a target block or passing `--var` flags only, zero code changes

### correctness
- [ ] all secrets from env vars, zero hardcoded values
- [ ] all SQL in `db/queries.py`, none inline in routers or services
- [ ] pydantic models for every request and response
- [ ] async connection pool with OAuth token refresh (app) / single OAuth connection (worker)
- [ ] proper HTTP status codes: 202 for submit, 200 for status/result, 404 for missing, 422 for validation
- [ ] lifespan handler for db_manager init/teardown (not deprecated @app.on_event)
- [ ] worker exits with code 0 on success, non-zero on unrecoverable error
- [ ] all state transitions logged with job_id and timestamp
- [ ] reconciler is idempotent — safe to run multiple times

### infrastructure
- [ ] bundle targets: dev (mode=development) and prod (mode=production)
- [ ] job names prefixed with [${bundle.target}]
- [ ] no new_cluster in any job — serverless only
- [ ] databricks-sdk>=0.81.0 in all requirements (required for w.postgres module)
- [ ] psycopg[binary]>=3.0 everywhere — NOT psycopg2 (psycopg3 required for async + hostaddr)
- [ ] FastAPI + uvicorn NOT in app/requirements.txt (pre-installed in Apps runtime)
- [ ] .gitignore covers: .env, __pycache__, *.pyc, .databricks/, dist/
- [ ] requirements.txt pins major versions only (lets Databricks env resolve patches)
- [ ] README documents: architecture, setup steps, env vars, deploy commands, test commands
