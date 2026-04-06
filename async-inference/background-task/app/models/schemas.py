from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1, max_length=50_000)


class SubmitRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1, max_length=100)
    max_tokens: int = Field(default=4096, ge=1, le=16384)
    caller_id: str | None = Field(default=None, max_length=255)


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
    result: str | None = None  # full text if DONE
    partial: str | None = None  # assembled chunks if STREAMING
    total_tokens: int | None = None
    latency_ms: int | None = None
    note: str | None = None
