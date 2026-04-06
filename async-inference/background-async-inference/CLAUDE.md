# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Production-grade async inference pattern on Databricks using in-process background tasks. Clients submit LLM jobs and poll for results — inference runs as `asyncio` background tasks directly inside the FastAPI app, with no external Lakeflow worker or reconciler.

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
# Deploy infrastructure + app
databricks bundle deploy --target dev

# Run schema migration (once, or after schema changes)
databricks bundle run schema_migration --target dev

# Start the app
databricks bundle run background_async_inference_app --target dev

# Get deployed app URL
databricks apps get background-async-inference-dev
```

### Initial Setup (first time)
```bash
databricks secrets create-scope background-async-inference
databricks secrets put-secret background-async-inference azure-openai-endpoint "..."
databricks secrets put-secret background-async-inference azure-openai-key "..."
databricks secrets put-secret background-async-inference azure-deployment-name "..."
```

## Architecture

```
Client → POST /jobs/submit → FastAPI App (Databricks Apps)
                                │
                                ├── INSERT job_requests (status=PENDING)
                                └── asyncio.create_task(inference.run(job_id, payload))
                                         │
                              [asyncio.Semaphore — max MAX_CONCURRENT_INFERENCES]
                                         │
                              asyncio.to_thread(_run_sync)
                                         │
                              ├── claim job (UPDATE WHERE status=PENDING)
                              ├── stream from Azure OpenAI / Foundation Model
                              ├── write chunks to job_chunks every N chars
                              └── mark DONE / FAILED in job_requests + job_results

App startup (crash recovery):
  → reset RUNNING/STREAMING → PENDING
  → re-spawn as background tasks

Client → GET /jobs/status/{id}  → current state
      → GET /jobs/result/{id}   → partial (STREAMING) or full (DONE) result
```

**State machine**: `PENDING → RUNNING → STREAMING → DONE | FAILED`

**Key difference from `long-running-inference`**: No Lakeflow worker job, no reconciler job. Inference runs inside the app process. On app restart, `_recover_in_flight_jobs()` resets and re-spawns any orphaned jobs.

### Components

| Directory | Purpose |
|-----------|---------|
| `app/` | FastAPI app (Databricks Apps runtime) |
| `app/services/inference.py` | Semaphore-bounded background task runner |
| `app/services/azure_openai.py` | Streaming inference with chunk buffering |
| `infra/schema/` | Postgres DDL + migration runner |
| `resources/` | DAB resource definitions (app, lakebase, experiment, schema job) |
| `tests/unit/` | pytest unit tests, no DB required |
| `tests/integration/` | End-to-end tests against live app |

### Key Files

- `databricks.yml` — DAB root config; all variables live here (zero code changes for new clients/environments)
- `app/db/connection.py` — `LakebaseConnectionManager`: async pool with OAuth refresh + `get_sync_conn()` for thread use
- `app/services/inference.py` — background task runner: semaphore → `asyncio.to_thread` → sync streaming
- `app/services/azure_openai.py` — streaming inference (sync, runs inside thread pool thread)
- `app/app.py` — lifespan: `initialize()` → `_recover_in_flight_jobs()` → serve
- `infra/schema/00_init.sql` — Three-table schema: `job_requests`, `job_chunks`, `job_results`

## Key Design Constraints

**All configuration must be DAB variables** — no hardcoded values in Python files. Every tunable is declared in `databricks.yml` and injected as env vars at deploy time.

**Thread safety for DB**: Route handlers use `session()` (creates connection on event loop thread) + `asyncio.to_thread(query_fn, conn)`. Background tasks use `get_sync_conn()` (creates connection inside the thread pool thread) — no thread-crossing.

**Concurrency**: Controlled by `asyncio.Semaphore(MAX_CONCURRENT_INFERENCES)` in `inference.py`. If all slots are busy, new tasks queue behind the semaphore until one finishes.

**Crash recovery**: On startup, `_recover_in_flight_jobs()` resets any RUNNING/STREAMING jobs (left by a previous app instance) back to PENDING and re-spawns them. There is a small window (~seconds) where a job could be lost if the app dies mid-reset.

**Streaming to DB**: Tokens buffered in memory and flushed to `job_chunks` every `FLUSH_EVERY` chars. Clients can read partial results while inference runs.

## Trade-offs vs `long-running-inference`

| Aspect | This pattern | long-running-inference |
|---|---|---|
| Cold-start latency | ~0ms (in-process) | ~10–30s (Lakeflow spin-up) |
| Crash recovery | Startup reset (small window) | Reconciler every 5 min |
| Scalability | Bounded by app instance | Lakeflow scales independently |
| Per-job logs | Mixed in app logs | Separate Lakeflow run logs |
| DAB resources | 3 (app, lakebase, experiment) | 5 (+ worker job, reconciler) |

## Dependencies

App (`app/requirements.txt`): `psycopg[binary]>=3.0`, `databricks-sdk>=0.81.0`, `openai`, `colorlog>=6.0`, `mlflow>=2.10.0`
`fastapi` and `uvicorn` are pre-installed in the Databricks Apps runtime — do not add them to requirements.txt.

## Targets

- `dev` — default development target
- `prod` — uses service principal (`run_as`) for M2M auth
