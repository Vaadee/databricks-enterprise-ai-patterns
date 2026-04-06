import asyncio
import logging

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db.connection import db_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    """Liveness check — always 200 if the process is alive."""
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    """
    Readiness check — verifies database connectivity before accepting traffic.
    Returns 503 if the DB is unreachable or the pool semaphore is exhausted (timeout).
    Load balancers and orchestrators should poll this before routing traffic.
    """
    try:
        async with asyncio.timeout(2.0):
            async with db_manager.session() as conn:
                await asyncio.to_thread(conn.execute, "SELECT 1")
        return {"status": "ready"}
    except (psycopg.OperationalError, asyncio.TimeoutError, Exception) as e:
        logger.warning("Readiness check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": str(e)},
        )
