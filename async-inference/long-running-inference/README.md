# long-running-inference

Async long-running document inference on Databricks. Clients submit a document and get back a `job_id` immediately — no waiting for the model. A serverless Lakeflow Job picks up the work, streams the response to Lakebase (Postgres), and clients poll for status and results.

Works with **Databricks Foundation Models** (zero secrets needed for testing) or **Azure OpenAI** for production.

Everything configurable at the DAB variable level — switch clients or environments with no code changes.

---

## Architecture

```
Client
  │
  │  POST /jobs/submit  →  202 + job_id
  │  GET  /jobs/status/{id}
  │  GET  /jobs/result/{id}
  ▼
┌─────────────────────────────┐
│   Databricks App (FastAPI)  │   ← OAuth M2M auth via service principal
│   app/                      │   ← psycopg3 + Lakebase OAuth token refresh
└────────────┬────────────────┘
             │ SDK: jobs.run_now(job_id)
             ▼
┌─────────────────────────────┐
│  Lakeflow Worker Job        │   ← serverless, one run per document
│  worker/worker.py           │   ← SELECT FOR UPDATE SKIP LOCKED claim
└────────────┬────────────────┘
             │ streaming tokens
             ▼
┌──────────────────────────┐     ┌──────────────────────────────┐
│  Foundation Models       │     │  Lakebase Autoscaling        │
│  (or Azure OpenAI GPT-4) │────▶│  job_requests / job_chunks   │
│  streaming               │     │  / job_results               │
└──────────────────────────┘     └──────────────────────────────┘
                                             ▲
                             ┌───────────────┘
                             │ every 5 min
                  ┌──────────────────────┐
                  │  Reconciler Job      │   ← serverless, re-triggers
                  │  infra/reconciler/   │     stale PENDING jobs
                  └──────────────────────┘
```

**State machine:** `PENDING → RUNNING → STREAMING → DONE | FAILED`

---

## Prerequisites

- Databricks workspace (AWS or Azure) with Lakebase Autoscaling enabled
- Databricks CLI **≥ 0.287.0** — required for `postgres_*` bundle resource types
  ```bash
  databricks --version
  # Upgrade: curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
  ```
- Authenticated CLI profile:
  ```bash
  databricks auth login --profile <your-profile>
  databricks auth status --profile <your-profile>
  ```
- For **Azure OpenAI** path only: an Azure OpenAI resource with a GPT-4 deployment

---

## Inference provider options

### Option A — Databricks Foundation Models (recommended for testing, no secrets needed)

The worker uses Databricks Foundation Models via the OpenAI-compatible serving endpoint.
Auth is automatic — `DATABRICKS_HOST` and `DATABRICKS_TOKEN` are injected by the runtime.

Pass the model name at deploy time:
```bash
--var="foundation_model_name=databricks-meta-llama-3-3-70b-instruct"
```

No secret scope required.

### Option B — Azure OpenAI

Create a secret scope and populate credentials (see [Step 2](#2-optional-azure-openai--create-and-populate-secret-scope) below).
Leave `foundation_model_name` empty (the default).

---

## Step-by-step setup

All commands run from this directory:
```bash
cd databricks-enterprise-ai-patterns/async-inference/long-running-inference
```

Set your CLI profile:
```bash
export PROFILE=your-profile-name   # used in all commands below
```

---

### 1. Deploy the bundle

The bundle creates the Lakebase Autoscaling project, worker job, reconciler job, and Databricks App in one step.

**Foundation Models path (no Azure secrets):**
```bash
databricks bundle deploy --profile $PROFILE \
  --var="foundation_model_name=databricks-meta-llama-3-3-70b-instruct"
```

**Azure OpenAI path:**
```bash
databricks bundle deploy --profile $PROFILE
```

> **Note:** `databricks bundle deploy` uploads files and creates/updates all resources. It does **not** restart the app. Always follow with `bundle run` (Step 4).

---

### 2. (Optional — Azure OpenAI) Create and populate secret scope

Skip this step if using Foundation Models.

```bash
databricks secrets create-scope long-running-inference --profile $PROFILE
```

```bash
databricks secrets put-secret long-running-inference azure-openai-endpoint \
  --string-value "https://<your-resource>.openai.azure.com/" \
  --profile $PROFILE

databricks secrets put-secret long-running-inference azure-openai-key \
  --string-value "<your-key>" \
  --profile $PROFILE

databricks secrets put-secret long-running-inference azure-deployment-name \
  --string-value "<your-deployment-name>" \
  --profile $PROFILE
```

---

### 3. Run the schema migration (once, after first deploy)

Creates the three Lakebase tables and provisions a Postgres role for the app's service principal.
This step is idempotent — safe to re-run after schema changes.

```bash
databricks bundle run schema_migration --profile $PROFILE
```

Expected output:
```
Connecting to project=long-running-inference branch=production db=databricks_postgres
Schema migration complete.
Created Postgres role and granted table access to app SP <uuid> (long-running-inference-dev)
```

> **Why is this needed?** Lakebase requires an explicit `databricks_create_role()` SQL call
> to map the app's service principal to a Postgres role. Without this, the SP's OAuth token
> is rejected at the database level even if it has project-level permissions.

---

### 4. Start the app

```bash
databricks bundle run long_running_inference_app --profile $PROFILE
```

> **Important:** You must run this command every time you redeploy (`bundle deploy`) to pick
> up code changes. `bundle deploy` alone uploads files but does not restart the running app.

---

### 5. Get the app URL

```bash
databricks apps get long-running-inference-dev --profile $PROFILE --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])"
```

Set it as a variable for the curl commands below:
```bash
export APP_URL=https://long-running-inference-dev-<workspace-id>.databricksapps.com
```

---

### 6. Get a bearer token

All API calls to a Databricks App require authentication:

```bash
export TOKEN=$(databricks auth token --profile $PROFILE --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
```

Tokens expire after 1 hour. Re-run this command when you get a `302` redirect response.

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

Save the job ID:
```bash
export JOB_ID=3fa85f64-5717-4562-b3fc-2c963f66afa6
```

---

### Poll status

```bash
curl -s $APP_URL/jobs/status/$JOB_ID \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Response:
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "STREAMING",
  "created_at": "2026-04-05T20:50:35Z",
  "updated_at": "2026-04-05T20:50:37Z"
}
```

Possible `status` values: `PENDING` → `RUNNING` → `STREAMING` → `DONE` | `FAILED`

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
  "partial": null,
  "total_tokens": 1823,
  "latency_ms": 42000,
  "note": null
}
```

Response when still `STREAMING` (partial result available while inference runs):
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "STREAMING",
  "result": null,
  "partial": "The Roman Empire began with...",
  "total_tokens": null,
  "latency_ms": null,
  "note": "inference in progress — poll again for final result"
}
```

---

### One-liner: submit and wait for result

```bash
TOKEN=$(databricks auth token --profile $PROFILE --output json | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])') \
  && JOB_ID=$(curl -s -X POST $APP_URL/jobs/submit \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $TOKEN" \
      -d '{"messages":[{"role":"user","content":"Say hello in one sentence"}],"max_tokens":50}' \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])") \
  && echo "Submitted: $JOB_ID" \
  && sleep 20 \
  && curl -s $APP_URL/jobs/result/$JOB_ID \
      -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

### Health check

```bash
curl -s $APP_URL/health -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Response:
```json
{"status": "ok"}
```

---

## Deploying to a different client or environment

Everything is a DAB variable. No code changes required.

**CLI override (no file changes):**
```bash
databricks bundle deploy --profile $PROFILE \
  --var="app_name=acme-inference" \
  --var="secret_scope=acme-inference" \
  --var="lakebase_project_id=acme-inference" \
  --var="notification_email=ops@acme.com" \
  --var="worker_max_concurrent_runs=100"

databricks bundle run schema_migration --profile $PROFILE
databricks bundle run long_running_inference_app --profile $PROFILE
```

**Named target in `databricks.yml` (version-controlled):**
```yaml
targets:
  acme-prod:
    mode: production
    workspace:
      host: https://acme.azuredatabricks.net
    variables:
      app_name: acme-inference
      secret_scope: acme-inference
      lakebase_project_id: acme-inference
      notification_email: ops@acme.com
      worker_max_concurrent_runs: "100"
```

```bash
databricks bundle deploy --target acme-prod
databricks bundle run schema_migration --target acme-prod
databricks bundle run long_running_inference_app --target acme-prod
```

---

## Deploying to prod

```bash
databricks bundle deploy --target prod --profile $PROFILE
databricks bundle run schema_migration --target prod --profile $PROFILE
databricks bundle run long_running_inference_app --target prod --profile $PROFILE
```

The `prod` target runs as a service principal (`run_as: service_principal_name`).

---

## Updating app code

`bundle deploy` uploads new code to the workspace but the running app does **not** restart automatically. Always follow a deploy with:

```bash
databricks bundle run long_running_inference_app --profile $PROFILE
```

This triggers a new app deployment and restarts with the latest code.

---

## DAB variables reference

All variables are declared in `databricks.yml`. Override any of them via `--var` or a target block.

| Variable | Default | Description |
|---|---|---|
| `app_name` | `long-running-inference` | Base name for all resources |
| `secret_scope` | `long-running-inference` | Secret scope for Azure OpenAI credentials |
| `service_principal` | `long-running-inference-sp` | SP name for prod `run_as` |
| `lakebase_project_id` | `long-running-inference` | Lakebase Autoscaling project ID |
| `lakebase_branch_id` | `production` | Lakebase branch |
| `lakebase_db_name` | `databricks_postgres` | Postgres database name |
| `foundation_model_name` | *(empty)* | Foundation Model endpoint name. Set to use instead of Azure OpenAI (e.g. `databricks-meta-llama-3-3-70b-instruct`) |
| `worker_max_concurrent_runs` | `50` | Max parallel worker job runs |
| `reconciler_cron` | `0 */5 * * * ?` | Reconciler schedule (every 5 min) |
| `flush_every` | `1000` | Chars buffered before writing a chunk to Lakebase |
| `token_refresh_secs` | `3000` | OAuth token refresh interval in seconds (app only) |
| `db_pool_size` | `5` | App connection pool size |
| `db_pool_max_overflow` | `10` | App pool max overflow |
| `azure_api_version` | `2024-02-01` | Azure OpenAI API version |
| `notification_email` | `your-team@company.com` | Email for job failure alerts |

---

## Monitoring & troubleshooting

### App logs
```bash
databricks apps logs long-running-inference-dev --profile $PROFILE
```

For specific errors:
```bash
databricks apps logs long-running-inference-dev --profile $PROFILE 2>&1 | grep -i "error\|failed\|exception" | tail -20
```

### Worker and reconciler runs

```bash
# List recent worker runs
databricks jobs list-runs --profile $PROFILE --output json \
  | python3 -c "
import sys,json
runs = json.load(sys.stdin).get('runs', [])
for r in runs[:10]:
    print(r['run_id'], r['state']['life_cycle_state'], r.get('state',{}).get('result_state',''), r.get('run_name',''))
"
```

Or browse in the Databricks UI:
- Worker runs: **Jobs → `[dev] long-running-inference-worker`**
- Reconciler runs: **Jobs → `[dev] long-running-inference-reconciler`**

### Check permissions on the Lakebase project
```bash
databricks api get /api/2.0/permissions/database-projects/long-running-inference \
  --profile $PROFILE --output json
```

The app's service principal should have `CAN_USE`. If it's missing, re-run the schema migration:
```bash
databricks bundle run schema_migration --profile $PROFILE
```

### Check app service principal
```bash
databricks apps get long-running-inference-dev --profile $PROFILE --output json \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('sp_id:', d.get('service_principal_id'), 'sp_name:', d.get('service_principal_name'))"
```

### Check for stuck jobs (Postgres)

Connect to Lakebase and run:
```sql
SELECT job_id, status, claimed_by, created_at, updated_at
FROM job_requests
WHERE status IN ('PENDING', 'RUNNING')
ORDER BY created_at;
```

PENDING jobs older than 3 minutes are automatically re-triggered by the reconciler.
FAILED jobs have their error message in the `error_msg` column of `job_requests`.

### Token expiry

App tokens refresh every 50 minutes. Worker tokens are generated fresh per run.
If you see `password authentication failed` in app logs, the Postgres role for the SP may need to be re-created — re-run the schema migration.

---

## Running tests

**Unit tests** (no Databricks connection needed):
```bash
pip install pytest
pytest tests/unit/ -v
```

**Integration tests** (requires deployed app):
```bash
APP_URL=https://long-running-inference-dev-<workspace-id>.databricksapps.com \
  pytest tests/integration/ -v
```

---

## Known quirks

| Quirk | Explanation |
|---|---|
| `bundle deploy` doesn't restart the app | Always follow with `bundle run long_running_inference_app`. Databricks Apps requires an explicit new deployment to pick up code changes. |
| `environment_variables` in serverless job YAML is silently dropped | DAB/Terraform drops task-level `environment_variables` for serverless `spark_python_task`. This project uses `spark_python_task.parameters` (CLI args) instead, injected into `os.environ` at entrypoint startup. |
| Empty string DAB variables panic Terraform when used in parameter arrays | Databricks renders empty-string variables as `null` in Terraform JSON arrays. `foundation_model_name` uses a job-level parameter (`{{job.parameters.foundation_model_name}}`) which handles empty strings correctly at runtime. |
| App SP needs a Postgres role, not just project-level permission | `CAN_USE` on the `database-projects` resource alone is insufficient. The `databricks_auth` extension must create a role via `databricks_create_role()` inside the database. The schema migration handles this automatically. |

---

## Repository layout

```
long-running-inference/
├── databricks.yml              # DAB config — all variables defined here
├── app/                        # Databricks App (FastAPI gateway)
│   ├── app.py                  # FastAPI entrypoint + lifespan + / → /docs redirect
│   ├── app.yaml                # App runtime config (uvicorn command + env vars)
│   ├── db/connection.py        # Lakebase OAuth pool + background token refresh
│   ├── db/queries.py           # All app SQL
│   ├── routers/jobs.py         # /submit /status /result
│   ├── routers/health.py       # /health
│   ├── services/job_trigger.py # Lakeflow trigger via SDK (uses WORKER_JOB_ID)
│   ├── models/schemas.py       # Pydantic request/response models
│   └── requirements.txt
├── worker/                     # Serverless Lakeflow Job
│   ├── worker.py               # Entrypoint — CLI args → os.environ → claim → stream
│   ├── db/connection.py        # Single-use OAuth connection (no pool)
│   ├── db/queries.py           # All worker SQL
│   ├── services/azure_openai.py # Streaming inference (Foundation Models or Azure)
│   └── requirements.txt
├── infra/
│   ├── schema/00_init.sql      # Lakebase schema DDL
│   ├── schema/migrate.py       # Migration runner + Postgres role provisioning
│   └── reconciler/reconciler.py # Re-triggers stale PENDING jobs every 5 min
├── resources/
│   ├── lakebase.yml            # DAB postgres_project resource
│   ├── app.yml                 # DAB app resource (job reference → WORKER_JOB_ID)
│   ├── worker_job.yml          # DAB worker job (serverless, CLI args for config)
│   ├── reconciler_job.yml      # DAB reconciler job (scheduled, CLI args for config)
│   └── schema_job.yml          # DAB one-time schema migration job
└── tests/
    ├── unit/                   # Schema + query unit tests (no DB needed)
    └── integration/            # End-to-end tests (requires deployed app)
```
