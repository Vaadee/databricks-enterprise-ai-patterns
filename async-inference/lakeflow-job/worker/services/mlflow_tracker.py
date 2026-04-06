"""
MLflow run logging for inference requests.

Logs one run per job to the configured experiment:
  - Tags:    job_id, status, caller_id (optional), error (on failure)
  - Params:  model, max_tokens
  - Metrics: total_tokens, prompt_tokens, completion_tokens, latency_ms, latency_s,
             tokens_per_second

Best-effort: all failures are caught and logged as warnings so they never
interrupt the main inference flow or alter job status in the DB.

Set MLFLOW_EXPERIMENT_NAME in the environment (via --mlflow_experiment_name CLI arg)
to enable tracking. If the variable is absent or empty, this module is a no-op.
"""

import logging
import os

logger = logging.getLogger(__name__)


def log_run(
    *,
    job_id: str,
    model: str,
    max_tokens: int,
    caller_id: str | None,
    total_tokens: int,
    prompt_tokens: int,
    latency_ms: int,
    status: str = "DONE",
    error: str | None = None,
) -> None:
    """
    Log one MLflow run for a completed or failed inference request.
    No-ops silently if MLFLOW_EXPERIMENT_NAME is not set or MLflow is unavailable.

    Args:
        job_id:          The Postgres job UUID (used as the MLflow run name).
        model:           Model/deployment name (Foundation Model or Azure deployment).
        max_tokens:      Requested max_tokens from the submit payload.
        caller_id:       Optional caller identifier from the submit payload.
        total_tokens:    Total tokens consumed (prompt + completion).
        prompt_tokens:   Tokens in the prompt (input side).
        latency_ms:      Wall-clock inference time in milliseconds.
        status:          "DONE" or "FAILED".
        error:           Error message (FAILED only, truncated to 500 chars).
    """
    experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME", "")
    if not experiment_name:
        return

    try:
        import mlflow
        from mlflow.tracking import MlflowClient

        mlflow.set_tracking_uri("databricks")

        # Use MlflowClient to get-or-create so logging works even if the DAB experiment
        # resource hasn't propagated yet or the name lookup returns None.
        _client = MlflowClient()
        _exp = _client.get_experiment_by_name(experiment_name)
        if _exp is None:
            _client.create_experiment(experiment_name)
        mlflow.set_experiment(experiment_name)

        completion_tokens = max(0, total_tokens - prompt_tokens)
        tokens_per_sec = (
            round(completion_tokens / (latency_ms / 1000.0), 2)
            if latency_ms > 0 and completion_tokens > 0
            else 0.0
        )
        mlflow_status = "FINISHED" if status == "DONE" else "FAILED"

        with mlflow.start_run(run_name=job_id):
            # ── Identity tags ─────────────────────────────────────────────
            mlflow.set_tag("job_id", job_id)
            mlflow.set_tag("status", status)
            if caller_id:
                mlflow.set_tag("caller_id", caller_id)
            if error:
                mlflow.set_tag("error", error[:500])  # cap length for UI readability

            # ── Inference params ──────────────────────────────────────────
            mlflow.log_param("model", model)
            mlflow.log_param("max_tokens", max_tokens)

            # ── Metrics ───────────────────────────────────────────────────
            mlflow.log_metric("total_tokens", total_tokens)
            mlflow.log_metric("prompt_tokens", prompt_tokens)
            mlflow.log_metric("completion_tokens", completion_tokens)
            mlflow.log_metric("latency_ms", latency_ms)
            mlflow.log_metric("latency_s", round(latency_ms / 1000.0, 3))
            mlflow.log_metric("tokens_per_second", tokens_per_sec)

            mlflow.end_run(status=mlflow_status)

        logger.info(
            "MLflow run logged: job_id=%s status=%s tokens=%d latency_ms=%d",
            job_id,
            status,
            total_tokens,
            latency_ms,
        )

    except Exception as exc:
        logger.warning(
            "MLflow logging failed for job_id=%s — %s: %s",
            job_id,
            type(exc).__name__,
            exc,
        )
