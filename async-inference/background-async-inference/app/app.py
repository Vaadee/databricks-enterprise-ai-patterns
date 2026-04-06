import asyncio
import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from db import queries
from db.connection import db_manager
from routers import health, jobs
from services import inference

# ── Colored logging setup ─────────────────────────────────────────────────────
try:
    import colorlog
    _handler = colorlog.StreamHandler()
    _handler.setFormatter(colorlog.ColoredFormatter(
        fmt="%(log_color)s%(asctime)s %(levelname)s %(name)s%(reset)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    logging.getLogger().addHandler(_handler)
    logging.getLogger().setLevel(logging.INFO)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

logger = logging.getLogger(__name__)


async def _recover_in_flight_jobs() -> None:
    """
    On app startup: reset any jobs left RUNNING or STREAMING from a previous app
    instance back to PENDING and re-spawn them as background inference tasks.
    """
    async with db_manager.session() as conn:
        jobs_to_recover = await asyncio.to_thread(queries.reset_in_flight_jobs, conn)
    for job in jobs_to_recover:
        asyncio.create_task(inference.run(str(job["job_id"]), job["payload"]))
    if jobs_to_recover:
        logger.warning("Startup recovery: re-spawned %d in-flight jobs", len(jobs_to_recover))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_manager.initialize()
    await _recover_in_flight_jobs()
    yield
    await db_manager.close()


app = FastAPI(
    title="background-async-inference",
    description="Async inference via in-process background tasks — no external Lakeflow worker",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(jobs.router, prefix="/jobs")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.exception_handler(psycopg.OperationalError)
async def db_operational_error_handler(request: Request, exc: psycopg.OperationalError):
    """Convert Postgres connectivity errors to 503 so load balancers and clients know to retry."""
    logger.error("DB operational error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please retry."},
    )
