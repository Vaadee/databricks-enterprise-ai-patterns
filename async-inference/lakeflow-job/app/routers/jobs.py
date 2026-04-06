import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException

from db import queries
from db.connection import db_manager
from models.schemas import (
    ResultResponse,
    StatusResponse,
    SubmitRequest,
    SubmitResponse,
)
from services.job_trigger import trigger_worker

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


@router.post("/submit", response_model=SubmitResponse, status_code=202)
async def submit(body: SubmitRequest):
    async with db_manager.session() as conn:
        row = await asyncio.to_thread(queries.insert_job, conn, body.model_dump())

    logger.info("Inserted job_id=%s", row["job_id"])

    # Best-effort trigger — reconciler handles any failures
    try:
        await asyncio.to_thread(trigger_worker, str(row["job_id"]))
    except Exception as e:
        logger.warning("Trigger failed for job_id=%s — %s: %s", row["job_id"], type(e).__name__, e)

    return SubmitResponse(**row)


@router.get("/status/{job_id}", response_model=StatusResponse)
async def status(job_id: UUID):
    async with db_manager.session() as conn:
        row = await asyncio.to_thread(queries.get_job_status, conn, str(job_id))

    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    return StatusResponse(**row)


@router.get("/result/{job_id}", response_model=ResultResponse)
async def result(job_id: UUID):
    async with db_manager.session() as conn:
        row = await asyncio.to_thread(queries.get_job_result, conn, str(job_id))

    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    return ResultResponse(**row)
