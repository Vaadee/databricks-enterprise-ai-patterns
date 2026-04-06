# background-async-inference

Async long-running inference on Databricks using **in-process background tasks** — no external Lakeflow worker or reconciler. Clients submit a document and get back a `job_id` immediately. The FastAPI app spawns an `asyncio` background task that streams the response directly to Lakebase (Postgres), and clients poll for status and results.

Works with **Databricks Foundation Models** (zero secrets needed for testing) or **Azure OpenAI** for production.

**Compare to [`long-running-inference`](../long-running-inference/)** — same API, same DB schema, different execution model:

| | `long-running-inference` | `background-async-inference` |
|---|---|---|
| Inference runs in | Lakeflow serverless job | asyncio task inside the app |
| Concurrency control | Lakeflow `max_concurrent_runs` | `asyncio.Semaphore` |
| Crash recovery | Reconciler job (every 5 min) | Startup recovery in app lifespan |
| Job cold-start | ~10–30s | ~0ms |
| DAB resources | 5 | 3 |

---

## Architecture

```
Client
  │
  │  POST /jobs/submit  →  202 + job_id
  │  GET  /jobs/status/{id}
  │  GET  /jobs/result/{id}
  ▼
┌─────────────────────────────────────────┐
│   Databricks App (FastAPI)              │   ← OAuth M2M via service principal
│                                         │
│   asyncio.create_task(inference.run())  │   ← spawned on submit, no cold-start
│   asyncio.Semaphore(MAX_CONCURRENT)     │   ← backpressure (default 20)
│   asyncio.to_thread(_run_sync)          │   ← keeps event loop free
└──────────────┬──────────────────────────┘
               │ streaming tokens
               ▼
┌──────────────────────────┐     ┌──────────────────────────────┐
│  Foundation Models       │     │  Lakebase Autoscaling        │
│  (or Azure OpenAI GPT-4) │────▶│  job_requests / job_chunks   │
│  streaming               │     │  / job_results               │
└──────────────────────────┘     └──────────────────────────────┘

App startup (crash recovery):
  → reset RUNNING/STREAMING → PENDING
  → re-spawn as background tasks
```

**State machine:** `PENDING → RUNNING → STREAMING → DONE | FAILED`

---

## Prerequisites

- Databricks workspace (AWS or Azure) with Lakebase Autoscaling enabled
- Databricks CLI **≥ 0.287.0**
  ```bash
  databricks --version
  ```
- Authenticated CLI profile:
  ```bash
  databricks auth login --profile <your-profile>
  ```
- For **Azure OpenAI** path only: an Azure OpenAI resource with a GPT-4 deployment

---

## Inference provider options

### Option A — Databricks Foundation Models (recommended, no secrets needed)

Set in `app/app.yaml`:
```yaml
- name: FOUNDATION_MODEL_NAME
  value: "databricks-meta-llama-3-3-70b-instruct"
```

Already set by default. No secret scope required.

### Option B — Azure OpenAI

Create a secret scope and populate credentials (see [Step 2](#2-optional-azure-openai--create-and-populate-secret-scope)).
Set `FOUNDATION_MODEL_NAME` to empty string in `app/app.yaml`.

---

## Step-by-step setup

All commands run from this directory:
```bash
cd databricks-enterprise-ai-patterns/async-inference/background-async-inference
export PROFILE=your-profile-name
```

---

### 1. Deploy the bundle

Creates the Lakebase project, MLflow experiment, schema job, and Databricks App.

```bash
databricks bundle deploy --profile $PROFILE
```

---

### 2. (Optional — Azure OpenAI) Create and populate secret scope

Skip this step if using Foundation Models.

```bash
databricks secrets create-scope background-async-inference --profile $PROFILE

databricks secrets put-secret background-async-inference azure-openai-endpoint \
  --string-value "https://<your-resource>.openai.azure.com/" --profile $PROFILE

databricks secrets put-secret background-async-inference azure-openai-key \
  --string-value "<your-key>" --profile $PROFILE

databricks secrets put-secret background-async-inference azure-deployment-name \
  --string-value "<your-deployment-name>" --profile $PROFILE
```

---

### 3. Run the schema migration (once, after first deploy)

Creates the three Lakebase tables, provisions a Postgres role for the app SP, and grants the app SP CAN_MANAGE on the MLflow experiment.

```bash
databricks bundle run schema_migration --profile $PROFILE
```

---

### 4. Start the app

```bash
databricks bundle run background_async_inference_app --profile $PROFILE
```

> **Important:** Re-run this after every `bundle deploy` to pick up code changes.

---

### 5. Get the app URL

```bash
databricks apps get background-async-inference-dev --profile $PROFILE --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])"

export APP_URL=https://background-async-inference-dev-<workspace-id>.databricksapps.com
```

---

### 6. Get a bearer token

```bash
export TOKEN=$(databricks auth token --profile $PROFILE --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
```

---

## API usage

### Submit a job

```bash
curl -s -X POST $APP_URL/jobs/submit \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Summarize the history of the Roman Empire in detail."}
    ],
    "max_tokens": 4096
  }' | python3 -m json.tool
```

Response `202`:
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "PENDING",
  "created_at": "2026-04-05T20:50:35Z"
}
```

---

### Poll status

```bash
curl -s $APP_URL/jobs/status/$JOB_ID \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Possible `status` values: `PENDING → RUNNING → STREAMING → DONE | FAILED`

---

### Fetch result

```bash
curl -s $APP_URL/jobs/result/$JOB_ID \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Response when `DONE`:
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "DONE",
  "result": "The Roman Empire began with the reign of Augustus...",
  "total_tokens": 1823,
  "latency_ms": 22000
}
```

---

## Smoke test

```bash
APP_URL=https://background-async-inference-dev-<workspace-id>.databricksapps.com \
PROFILE=your-profile \
python smoke_test.py
```

Runs 26 checks: health, readiness, input validation, 404s, short job e2e, long job with STREAMING observation, concurrent submits.

---

## Deploying to a different client or environment

No code changes required — everything is a DAB variable.

**CLI override:**
```bash
databricks bundle deploy --profile $PROFILE \
  --var="app_name=acme-bg-inference" \
  --var="lakebase_project_id=acme-bg-inference" \
  --var="max_concurrent_inferences=50"

databricks bundle run schema_migration --profile $PROFILE
databricks bundle run background_async_inference_app --profile $PROFILE
```

**Named target in `databricks.yml`:**
```yaml
targets:
  acme-prod:
    mode: production
    workspace:
      host: https://acme.azuredatabricks.net
    variables:
      app_name: acme-bg-inference
      lakebase_project_id: acme-bg-inference
      max_concurrent_inferences: "50"
      notification_email: ops@acme.com
```

---

## DAB variables reference

| Variable | Default | Description |
|---|---|---|
| `app_name` | `background-async-inference` | Base name for all resources |
| `secret_scope` | `background-async-inference` | Secret scope for Azure OpenAI credentials |
| `service_principal` | `background-async-inference-sp` | SP name for prod `run_as` |
| `lakebase_project_id` | `background-async-inference` | Lakebase Autoscaling project ID |
| `lakebase_branch_id` | `production` | Lakebase branch |
| `lakebase_db_name` | `databricks_postgres` | Postgres database name |
| `max_concurrent_inferences` | `20` | asyncio.Semaphore limit for in-process concurrency |
| `flush_every` | `1000` | Chars buffered before writing a chunk to Lakebase |
| `token_refresh_secs` | `3000` | OAuth token refresh interval in seconds |
| `db_pool_size` | `5` | Route handler connection pool size |
| `azure_api_version` | `2024-02-01` | Azure OpenAI API version |
| `notification_email` | `your-team@company.com` | Email for job failure alerts |

---

## Monitoring & troubleshooting

### App logs
```bash
databricks apps logs background-async-inference-dev -p $PROFILE
databricks apps logs background-async-inference-dev -p $PROFILE 2>&1 | grep -i "error\|failed\|exception"
```

### Check for stuck jobs
```sql
SELECT job_id, status, claimed_by, created_at, updated_at
FROM job_requests
WHERE status IN ('PENDING', 'RUNNING', 'STREAMING')
ORDER BY created_at;
```

On app restart, any RUNNING/STREAMING jobs are automatically reset to PENDING and re-spawned.

### Token expiry
If you see `password authentication failed` in app logs, re-run the schema migration to re-provision the Postgres role.

---

## Repository layout

```
background-async-inference/
├── databricks.yml              # DAB config — all variables defined here
├── app/                        # Databricks App (FastAPI gateway + inference)
│   ├── app.py                  # FastAPI + lifespan (init + crash recovery)
│   ├── app.yaml                # App runtime config (uvicorn + env vars)
│   ├── db/connection.py        # Lakebase OAuth pool + get_sync_conn() for threads
│   ├── db/queries.py           # All SQL (route handlers + inference tasks)
│   ├── routers/jobs.py         # /submit /status /result
│   ├── routers/health.py       # /health /ready
│   ├── services/inference.py   # Semaphore + asyncio.to_thread background runner
│   ├── services/azure_openai.py # Streaming inference (Foundation Models or Azure)
│   ├── services/mlflow_tracker.py # MLflow run logging (best-effort)
│   ├── models/schemas.py       # Pydantic request/response models
│   └── requirements.txt
├── infra/
│   ├── schema/00_init.sql      # Lakebase schema DDL
│   └── schema/migrate.py       # Migration runner + Postgres role + MLflow permissions
├── resources/
│   ├── lakebase.yml            # DAB postgres_project resource
│   ├── app.yml                 # DAB app resource
│   ├── mlflow_experiment.yml   # DAB MLflow experiment resource
│   └── schema_job.yml          # DAB one-time schema migration job
└── tests/
    ├── unit/                   # Schema + query unit tests (no DB needed)
    └── integration/            # End-to-end tests (requires deployed app)
```
