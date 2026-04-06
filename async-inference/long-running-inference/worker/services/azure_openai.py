"""
Streaming Azure OpenAI inference.

Buffers tokens until FLUSH_EVERY chars, then writes a chunk to Lakebase.
All config from environment variables injected by DAB.
Logs one MLflow run per job on completion or failure (best-effort, no-op if unset).
"""

import logging
import os
import time

import psycopg
from openai import AzureOpenAI, OpenAI

from db import queries
from services.mlflow_tracker import log_run

logger = logging.getLogger(__name__)

FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY", "1000"))


def _build_client() -> tuple:
    """Returns (client, model_name). Uses Databricks Foundation Models if
    FOUNDATION_MODEL_NAME is set, otherwise falls back to Azure OpenAI."""
    fm = os.environ.get("FOUNDATION_MODEL_NAME", "")
    if fm:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        # DATABRICKS_TOKEN is not auto-injected in serverless jobs; use the SDK
        # to get the current bearer token (works for PAT, OAuth M2M, and job identity).
        token = w.config.authenticate().get("Authorization", "").removeprefix("Bearer ")
        client = OpenAI(
            api_key=token,
            base_url=f"{w.config.host}/serving-endpoints",
        )
        return client, fm
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version=os.environ.get("AZURE_API_VERSION", "2024-02-01"),
        timeout=1800,  # 30 min ceiling — well above 15 min jobs
    )
    return client, os.environ["AZURE_DEPLOYMENT_NAME"]


def stream_completion(conn: psycopg.Connection, job_id: str, payload: dict) -> None:  # noqa: C901
    """
    Stream a completion from Azure OpenAI or Databricks Foundation Models,
    writing chunks to Lakebase as they arrive.
    Marks the job STREAMING on first token, DONE on completion, FAILED on any error.
    Logs one MLflow run per job (best-effort — never raises).
    Re-raises on failure so Lakeflow logs the exception and marks the run as failed.
    """
    client, deployment = _build_client()
    is_foundation_model = bool(os.environ.get("FOUNDATION_MODEL_NAME", ""))

    messages = payload.get("messages", [])
    max_tokens = payload.get("max_tokens", 4096)
    caller_id = payload.get("caller_id")

    buffer = ""
    chunk_index = 0
    full_text_parts: list[str] = []
    marked_streaming = False
    total_tokens = 0
    prompt_tokens = 0
    start_ms = int(time.time() * 1000)

    try:
        create_kwargs = dict(
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        # stream_options is Azure OpenAI-specific; Foundation Models rejects it
        if not is_foundation_model:
            create_kwargs["stream_options"] = {"include_usage": True}

        stream = client.chat.completions.create(**create_kwargs)

        for event in stream:
            # Usage is delivered in the final chunk
            if event.usage:
                total_tokens = event.usage.total_tokens
                prompt_tokens = event.usage.prompt_tokens

            if not event.choices:
                continue

            delta = event.choices[0].delta
            if not delta or not delta.content:
                continue

            token = delta.content

            if not marked_streaming:
                queries.mark_streaming(conn, job_id)
                marked_streaming = True
                logger.info("job_id=%s STREAMING started", job_id)

            buffer += token
            full_text_parts.append(token)

            if len(buffer) >= FLUSH_EVERY:
                queries.write_chunk(conn, job_id, chunk_index, buffer)
                logger.debug("job_id=%s chunk=%d flushed (%d chars)", job_id, chunk_index, len(buffer))
                chunk_index += 1
                buffer = ""

        # Flush remainder
        if buffer:
            queries.write_chunk(conn, job_id, chunk_index, buffer)

        full_text = "".join(full_text_parts)
        latency_ms = int(time.time() * 1000) - start_ms

        queries.mark_done(conn, job_id, full_text, total_tokens, prompt_tokens, latency_ms)
        logger.info(
            "job_id=%s DONE tokens=%d latency_ms=%d",
            job_id, total_tokens, latency_ms,
        )

        log_run(
            job_id=job_id,
            model=deployment,
            max_tokens=max_tokens,
            caller_id=caller_id,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            latency_ms=latency_ms,
            status="DONE",
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        latency_ms = int(time.time() * 1000) - start_ms
        logger.exception("job_id=%s FAILED: %s", job_id, error_msg)
        queries.mark_failed(conn, job_id, error_msg)

        log_run(
            job_id=job_id,
            model=deployment,
            max_tokens=max_tokens,
            caller_id=caller_id,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            latency_ms=latency_ms,
            status="FAILED",
            error=error_msg,
        )

        raise
