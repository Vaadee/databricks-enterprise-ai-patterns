# background-task

Async inference on Databricks using **in-process asyncio background tasks** — no external Lakeflow worker or reconciler. Clients submit a document and get back a `job_id` immediately. The FastAPI app spawns an `asyncio` background task that streams the response directly to Lakebase (Postgres), and clients poll for status and results.

Works with **Databricks Foundation Models** (zero secrets needed for testing) or **Azure OpenAI** for production.

**Compare to [`lakeflow-job`](../lakeflow-job/)** — same API, same DB schema, different execution model:

| | `lakeflow-job` | `background-task` |
|---|---|---|
| Inference runs in | Lakeflow serverless job | asyncio task inside the app |
| Concurrency control | Lakeflow `max_concurrent_runs` | `asyncio.Semaphore` |
| Crash recovery | Reconciler job (every 5 min) | Startup recovery in app lifespan |
| Job cold-start | ~10–30s | ~0ms |
| DAB resources | 5 | 3 |

## Inspiration

This pattern was inspired by the Databricks App Templates [OpenAI Agents SDK Long-Running Agent](https://github.com/databricks/app-templates/tree/main/agent-openai-agents-sdk-long-running-agent).

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

- Databricks workspace (AWS or Azure) with **Lakebase Autoscaling** enabled
- Databricks CLI **≥ 0.287.0** — check with `databricks --version`
- An authenticated CLI profile:
  ```bash
  databricks auth login --profile <your-profile>
  ```
- **Inference provider** — choose one:
  - **Databricks Foundation Models** *(recommended, zero secrets needed)* — default, works out of the box
  - **Azure OpenAI** — requires an Azure OpenAI resource with a GPT-4 deployment; `start_demo.sh` will prompt for credentials

---

## Getting started

All commands run from this directory:

```bash
cd databricks-enterprise-ai-patterns/async-inference/background-task
```

`start_demo.sh` handles everything end-to-end: bundle deploy, schema migration, app start, and prints your URL + token ready to use. It prompts for your Databricks profile, inference provider, and app name (defaults work for most cases).

```bash
# Interactive — prompts for profile, provider, app name
bash start_demo.sh

# Skip the profile prompt
bash start_demo.sh --profile <your-profile>

# Deploy + start + run smoke test automatically
bash start_demo.sh --profile <your-profile> --smoke
```

At the end you'll see a summary with the app URL, a masked token, and ready-to-paste `export` commands.

### Stop the demo

```bash
# Stop the app, keep all resources (Lakebase data, MLflow runs intact)
bash stop_demo.sh --profile <your-profile>

# Stop + permanently destroy the app, Lakebase project, MLflow experiment, Postgres data
bash stop_demo.sh --profile <your-profile> --destroy
```

---

<details>
<summary><strong>Manual setup (without demo scripts)</strong></summary>

```bash
export PROFILE=your-profile-name
```

**1. Deploy the bundle**
```bash
databricks bundle deploy --profile $PROFILE
```

**2. (Optional — Azure OpenAI only) Create and populate secret scope**
```bash
databricks secrets create-scope background-task --profile $PROFILE
databricks secrets put-secret background-task azure-openai-endpoint \
  --string-value "https://<your-resource>.openai.azure.com/" --profile $PROFILE
databricks secrets put-secret background-task azure-openai-key \
  --string-value "<your-key>" --profile $PROFILE
databricks secrets put-secret background-task azure-deployment-name \
  --string-value "<your-deployment-name>" --profile $PROFILE
```

**3. Run schema migration** *(once, or after schema changes)*
```bash
databricks bundle run schema_migration --profile $PROFILE
```

**4. Start the app** *(re-run after every `bundle deploy`)*
```bash
databricks bundle run background_task_app --profile $PROFILE
```

**5. Get the app URL**
```bash
export APP_URL=$(databricks apps get background-task-dev --profile $PROFILE --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
echo "APP_URL=$APP_URL"
```

**6. Get a bearer token**
```bash
export TOKEN=$(databricks auth token --profile $PROFILE --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
```

</details>

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
  }' | tee /tmp/submit_response.json | python3 -m json.tool

export JOB_ID=$(python3 -c 'import json; print(json.load(open("/tmp/submit_response.json"))["job_id"])')
echo "JOB_ID=$JOB_ID"
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

Runs 26 checks: health, readiness, input validation, 404s, short job e2e, long job with STREAMING observation, concurrent submits.

```bash
# Python (primary)
APP_URL=$APP_URL PROFILE=$PROFILE python3 smoke_test.py

# Shell / curl fallback (e.g. if Python SSL issues on macOS)
APP_URL=$APP_URL PROFILE=$PROFILE bash smoke_test.sh
```

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
databricks bundle run background_task_app --profile $PROFILE
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
| `app_name` | `background-task` | Base name for all resources |
| `secret_scope` | `background-task` | Secret scope for Azure OpenAI credentials |
| `service_principal` | `background-task-sp` | SP name for prod `run_as` |
| `lakebase_project_id` | `background-task` | Lakebase Autoscaling project ID |
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
databricks apps logs background-task-dev -p $PROFILE
databricks apps logs background-task-dev -p $PROFILE 2>&1 | grep -i "error\|failed\|exception"
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

### Output guardrails (workspace policy)
If you see `output_guardrails is not supported in streaming` in app logs, the workspace has Output Guardrails (PII detection, toxicity filters) enabled at the admin level on serving endpoints. The app automatically detects this and retries the request without streaming — the job will still complete, but tokens won't be flushed incrementally to `job_chunks`.

---

## Repository layout

```
background-task/
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
