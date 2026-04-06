"""
Unit tests for Pydantic request/response schemas.
"""

import pytest
from pydantic import ValidationError
from app.models.schemas import (
    Message,
    SubmitRequest,
    SubmitResponse,
    StatusResponse,
    ResultResponse,
)
from uuid import uuid4
from datetime import datetime, timezone


# ── Message ──────────────────────────────────────────────────────────────────

def test_message_valid_roles():
    for role in ("system", "user", "assistant"):
        m = Message(role=role, content="hello")
        assert m.role == role


def test_message_invalid_role():
    with pytest.raises(ValidationError):
        Message(role="unknown", content="hello")


def test_message_empty_content():
    with pytest.raises(ValidationError):
        Message(role="user", content="")


# ── SubmitRequest ─────────────────────────────────────────────────────────────

def test_submit_request_valid():
    req = SubmitRequest(messages=[{"role": "user", "content": "Summarize this."}])
    assert req.max_tokens == 4096
    assert req.caller_id is None


def test_submit_request_custom_tokens():
    req = SubmitRequest(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=8192,
        caller_id="test-client",
    )
    assert req.max_tokens == 8192
    assert req.caller_id == "test-client"


def test_submit_request_empty_messages():
    with pytest.raises(ValidationError):
        SubmitRequest(messages=[])


def test_submit_request_max_tokens_out_of_range():
    with pytest.raises(ValidationError):
        SubmitRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=0)
    with pytest.raises(ValidationError):
        SubmitRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=99999)


# ── ResultResponse ────────────────────────────────────────────────────────────

def test_result_response_done():
    r = ResultResponse(
        job_id=uuid4(),
        status="DONE",
        result="The answer is 42.",
        total_tokens=100,
        latency_ms=5000,
    )
    assert r.result == "The answer is 42."
    assert r.partial is None


def test_result_response_streaming():
    r = ResultResponse(
        job_id=uuid4(),
        status="STREAMING",
        partial="The answer is",
        note="inference in progress",
    )
    assert r.partial == "The answer is"
    assert r.result is None


def test_result_response_pending():
    r = ResultResponse(job_id=uuid4(), status="PENDING")
    assert r.result is None
    assert r.partial is None
