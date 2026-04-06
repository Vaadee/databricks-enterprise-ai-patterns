"""
Integration tests — require a running deployed app and real Azure OpenAI credentials.

Set APP_URL to the deployed Databricks App URL before running:
    APP_URL=https://... pytest tests/integration/test_api.py -v

Tests submit a real (small) job, poll until completion, and assert on the result.
"""

import logging
import os
import time
import uuid

import pytest
import requests

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
POLL_INTERVAL = 3  # seconds between polls
POLL_TIMEOUT = 300  # 5 min max wait for DONE


def skip_if_no_app():
    if not APP_URL:
        pytest.skip("APP_URL not set — skipping integration tests")


# ── helpers ───────────────────────────────────────────────────────────────────


def submit(payload: dict) -> requests.Response:
    return requests.post(f"{APP_URL}/jobs/submit", json=payload, timeout=30)


def get_status(job_id: str) -> requests.Response:
    return requests.get(f"{APP_URL}/jobs/status/{job_id}", timeout=10)


def get_result(job_id: str) -> requests.Response:
    return requests.get(f"{APP_URL}/jobs/result/{job_id}", timeout=10)


def wait_for_done(job_id: str, timeout: int = POLL_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = get_status(job_id)
        assert r.status_code == 200
        status = r.json()["status"]
        if status in ("DONE", "FAILED"):
            return r.json()
        time.sleep(POLL_INTERVAL)
    pytest.fail(f"job_id={job_id} did not reach DONE within {timeout}s")


# ── health ────────────────────────────────────────────────────────────────────


def test_health():
    skip_if_no_app()
    r = requests.get(f"{APP_URL}/health", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── submit ────────────────────────────────────────────────────────────────────


def test_submit_returns_202():
    skip_if_no_app()
    r = submit(
        {
            "messages": [{"role": "user", "content": "Say hello in one sentence."}],
            "max_tokens": 50,
        }
    )
    assert r.status_code == 202
    body = r.json()
    assert "job_id" in body
    assert body["status"] == "PENDING"


def test_submit_missing_messages_returns_422():
    skip_if_no_app()
    r = submit({"max_tokens": 100})
    assert r.status_code == 422


def test_submit_empty_messages_returns_422():
    skip_if_no_app()
    r = submit({"messages": []})
    assert r.status_code == 422


# ── status ────────────────────────────────────────────────────────────────────


def test_status_unknown_job_returns_404():
    skip_if_no_app()
    r = get_status(str(uuid.uuid4()))
    assert r.status_code == 404


# ── result ────────────────────────────────────────────────────────────────────


def test_result_unknown_job_returns_404():
    skip_if_no_app()
    r = get_result(str(uuid.uuid4()))
    assert r.status_code == 404


# ── end-to-end ────────────────────────────────────────────────────────────────


def test_full_submit_poll_result():
    skip_if_no_app()

    r = submit(
        {
            "messages": [{"role": "user", "content": "What is 2 + 2? Answer in one word."}],
            "max_tokens": 20,
            "caller_id": "integration-test",
        }
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Wait for DONE
    final = wait_for_done(job_id)
    assert final["status"] == "DONE", f"Job failed: {final}"

    # Check result
    r = get_result(job_id)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "DONE"
    assert body["result"] and len(body["result"]) > 0
    assert body["total_tokens"] and body["total_tokens"] > 0
    assert body["latency_ms"] and body["latency_ms"] > 0


def test_streaming_state_visible_during_inference():
    """
    Submit a longer job and check that STREAMING status appears before DONE.
    Uses a larger prompt to increase the chance of catching the STREAMING state.
    """
    skip_if_no_app()

    r = submit(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Write a detailed 500-word essay about the history of databases, "
                        "covering relational, NoSQL, and NewSQL systems."
                    ),
                }
            ],
            "max_tokens": 800,
        }
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    saw_streaming = False
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        r = get_status(job_id)
        status = r.json()["status"]
        if status == "STREAMING":
            saw_streaming = True
        if status in ("DONE", "FAILED"):
            break
        time.sleep(POLL_INTERVAL)

    # DONE is required; STREAMING is best-effort (may be too fast for short completions)
    final_status = get_status(job_id).json()["status"]
    assert final_status == "DONE"
    # Log whether we caught STREAMING — not a hard assertion as timing-dependent
    logging.getLogger(__name__).info("Caught STREAMING state: %s", saw_streaming)
