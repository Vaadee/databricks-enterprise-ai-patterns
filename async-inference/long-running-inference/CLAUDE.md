# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Production-grade async long-running inference pattern on Databricks. Clients submit LLM jobs and poll for results — the system handles Azure OpenAI streaming, state management in Lakebase (managed Postgres), and reliability via a reconciler.

## Commands

### Testing
```bash
# Unit tests (no DB or deployment needed)
pytest tests/unit/ -v

# Integration tests (requires a deployed app)
APP_URL=https://<app-url>.databricksapps.com pytest tests/integration/ -v
```

### Deployment (Databricks CLI ≥ 0.278.0 required)
```bash
# Schema migration (run once, or after schema changes)
pip install "databricks-sdk>=0.81.0" "psycopg[binary]>=3.0"
python infra/schema/migrate.py

# Deploy infrastructure
databricks bundle deploy --target dev

# Start the app
databricks bundle run long_running_inference_app --target dev

# Get deployed app URL
databricks apps get long-running-inference-dev
```

### Initial Setup (first time)
```bash
databricks postgres create-project --project-id long-running-inference
databricks secrets create-scope long-running-inference
databricks secrets put-secret long-running-inference azure-openai-endpoint "..."
databricks secrets put-secret long-running-inference azure-openai-key "..."
databricks secrets put-secret long-running-inference azure-deployment-name "..."
```

## Architecture

```
Client → POST /jobs/submit → FastAPI App (Databricks Apps)
                                │
                                ├── INSERT job_requests (status=PENDING)
                                └── trigger Worker Job (fire-and-forget)

Worker (Serverless Lakeflow Job, one run per job_id)
  → SELECT FOR UPDATE SKIP LOCKED (atomic claim, prevents double-processing)
  → Stream from Azure OpenAI GPT-4
  → Write chunks to job_chunks every N chars (default 1000)
  → Write final result to job_results, set status=DONE (or FAILED)

Reconciler (Scheduled Job, every 5 min)
  → Re-triggers PENDING jobs older than 3 min (safety net for trigger failures)

Client → GET /jobs/status/{id}  → current state
      → GET /jobs/result/{id}   → partial (STREAMING) or full (DONE) result
```

**State machine**: `PENDING → RUNNING → STREAMING → DONE | FAILED`

### Components

| Directory | Purpose |
|-----------|---------|
| `app/` | FastAPI app (Databricks Apps runtime) |
| `worker/` | Serverless Lakeflow job (one run per inference request) |
| `infra/schema/` | Postgres DDL + migration runner |
| `infra/reconciler/` | Scheduled job for stale PENDING jobs |
| `resources/` | DAB resource definitions (app, jobs) |
| `tests/unit/` | pytest unit tests, no DB required |
| `tests/integration/` | End-to-end tests against live app |

### Key Files

- `databricks.yml` — DAB root config; all 26+ configurable variables live here (zero code changes for new clients/environments)
- `app/db/connection.py` — `LakebaseConnectionManager`: async pool with background OAuth token refresh every 50 min
- `worker/worker.py` — Job entrypoint: claim → stream → persist → mark done/failed
- `worker/services/azure_openai.py` — Streaming inference with chunk buffering
- `infra/schema/00_init.sql` — Three-table schema: `job_requests`, `job_chunks`, `job_results`

## Key Design Constraints

**All configuration must be DAB variables** — no hardcoded values in Python files, no code changes to switch client workspaces. Every tunable (DB pool size, flush interval, cron schedule, resource names, secret scope) is declared in `databricks.yml` and injected as env vars at deploy time.

**OAuth everywhere** — No static DB passwords. The app refreshes tokens in a background asyncio task; the worker uses a single token for its short-lived run.

**Streaming to DB** — Tokens are buffered in memory and flushed to `job_chunks` every `flush_every` chars. Clients can read partial results while inference runs.

**`SELECT FOR UPDATE SKIP LOCKED`** — Workers atomically claim jobs; multiple concurrent workers cannot double-process the same job.

## Dependencies

App (`app/requirements.txt`): `psycopg[binary]>=3.0`, `databricks-sdk>=0.81.0`
Worker (`worker/requirements.txt`): `psycopg[binary]>=3.0`, `openai`, `databricks-sdk>=0.81.0`
`fastapi` and `uvicorn` are pre-installed in the Databricks Apps runtime — do not add them to requirements.txt.

## Targets

- `dev` — default development target
- `prod` — uses service principal (`run_as`) for M2M auth

Custom client targets are added to the `targets` block in `databricks.yml` with overrides for workspace URL, app_name, secret_scope, etc.
