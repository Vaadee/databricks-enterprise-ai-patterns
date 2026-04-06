"""
Lakebase Autoscaling connection manager for the FastAPI app.

OAuth tokens expire after 1 hour. A background asyncio task refreshes the token
every TOKEN_REFRESH_SECS (default 3000s / 50 min) before expiry. The token is
injected into each new psycopg.connect() call so connections always use a fresh token.

All configuration read from environment variables injected by DAB at deploy time.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import psycopg
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def _resolve_endpoint(w: WorkspaceClient, project_id: str, branch_id: str) -> tuple[str, str]:
    """Return (endpoint_name, host) for the primary R/W endpoint."""
    ep_parent = f"projects/{project_id}/branches/{branch_id}"
    endpoints = list(w.postgres.list_endpoints(parent=ep_parent))
    if not endpoints:
        raise RuntimeError(f"No endpoints found for {ep_parent}")
    ep_name = endpoints[0].name
    endpoint = w.postgres.get_endpoint(name=ep_name)
    return ep_name, endpoint.status.hosts.host


def _generate_token(w: WorkspaceClient, ep_name: str) -> str:
    cred = w.postgres.generate_database_credential(endpoint=ep_name)
    return cred.token


class LakebaseConnectionManager:
    """
    Manages psycopg3 connections to Lakebase Autoscaling with automatic OAuth
    token refresh. Uses a semaphore-bounded per-request connection pattern.

    Thread-safety note: psycopg3 Connection objects are NOT thread-safe. This
    manager calls psycopg.connect() synchronously in session() (not via
    asyncio.to_thread) so the connection is created on the event loop thread.
    Route handlers then pass the connection to asyncio.to_thread() for DB queries —
    one thread at a time, never concurrently — which is safe for psycopg3.
    """

    def __init__(self):
        self._project_id: str = os.environ["LAKEBASE_PROJECT_ID"]
        self._branch_id: str = os.environ.get("LAKEBASE_BRANCH_ID", "production")
        self._db_name: str = os.environ.get("LAKEBASE_DB_NAME", "databricks_postgres")
        self._pool_size: int = int(os.environ.get("DB_POOL_SIZE", "5"))
        self._refresh_secs: int = int(os.environ.get("TOKEN_REFRESH_SECS", "3000"))

        self._w: Optional[WorkspaceClient] = None
        self._ep_name: Optional[str] = None
        self._host: Optional[str] = None
        self._username: Optional[str] = None
        self._token: Optional[str] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._token_refresh_failures: int = 0

    async def initialize(self) -> None:
        self._w = WorkspaceClient()
        self._ep_name, self._host = await asyncio.to_thread(
            _resolve_endpoint, self._w, self._project_id, self._branch_id
        )
        self._username = (await asyncio.to_thread(self._w.current_user.me)).user_name
        self._token = await asyncio.to_thread(_generate_token, self._w, self._ep_name)
        self._semaphore = asyncio.Semaphore(self._pool_size)
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "Lakebase connection manager initialized: project=%s branch=%s",
            self._project_id,
            self._branch_id,
        )

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_secs)
            try:
                self._token = await asyncio.to_thread(_generate_token, self._w, self._ep_name)
                self._token_refresh_failures = 0
                logger.info("Lakebase OAuth token refreshed")
            except Exception:
                self._token_refresh_failures += 1
                if self._token_refresh_failures >= 3:
                    logger.error(
                        "Token refresh has failed %d consecutive times. "
                        "New connections will fail when the current token expires. "
                        "Check Databricks workspace connectivity and service principal credentials.",  # noqa: E501
                        self._token_refresh_failures,
                    )
                else:
                    logger.exception(
                        "Token refresh failed (attempt %d) — will retry next cycle",
                        self._token_refresh_failures,
                    )

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[psycopg.Connection, None]:
        """
        Yield a psycopg3 connection. Acquires the pool semaphore, opens a connection
        synchronously (fast, ~ms), and closes it on exit.

        psycopg.connect() is called synchronously here — NOT via asyncio.to_thread —
        so the connection object is created on the event loop thread. Route handlers
        then use asyncio.to_thread(queries.X, conn, ...) for the actual DB operations,
        which runs them in a single thread pool thread sequentially (safe for psycopg3).
        """
        async with self._semaphore:
            conn = psycopg.connect(
                host=self._host,
                dbname=self._db_name,
                user=self._username,
                password=self._token,
                sslmode="require",
                connect_timeout=30,  # fail fast if Lakebase endpoint is unreachable
            )
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    async def close(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("Lakebase connection manager closed")


# Module-level singleton — initialised in app lifespan
db_manager = LakebaseConnectionManager()
