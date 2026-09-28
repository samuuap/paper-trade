"""Application entry point.

Composition root: it wires the HTTP surface to the orchestrator and owns the
lifecycle of everything with a socket or a thread behind it.

The orchestrator runs in-process, as a task on the same event loop as the API,
rather than as a separate service. For a single-machine system that is the
right trade: the dashboard can read the scheduler's live state directly, there
is no queue or RPC to keep in sync, and there is exactly one process to start
and stop. `RUN_ORCHESTRATOR=false` detaches it, which is how you get a
read-only API pointed at the same database.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.base import dispose_engine
from app.llm.client import get_client
from app.market.provider import get_provider
from app.orchestrator.scheduler import get_orchestrator

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info(
        "app.starting",
        env=settings.env,
        orchestrator=settings.run_orchestrator,
        replay=settings.replay_mode,
        offline=settings.market_data_offline,
    )

    if settings.run_orchestrator:
        await get_orchestrator().start()

    try:
        yield
    finally:
        # Shutdown is best-effort and ordered: stop producing work, then close
        # the clients it was using, then drop the pool.
        if settings.run_orchestrator:
            with contextlib.suppress(Exception):
                await get_orchestrator().stop()
        with contextlib.suppress(Exception):
            await get_client().aclose()
        with contextlib.suppress(Exception):
            await get_provider().aclose()
        with contextlib.suppress(Exception):
            await dispose_engine()
        log.info("app.stopped")


app = FastAPI(
    title="Paper Trade Arena",
    description=(
        "Competing LLM agents trading US equities with simulated money, "
        "instrumented end to end."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", tags=["system"])
async def root() -> dict[str, Any]:
    return {
        "name": "paper-trade",
        "version": app.version,
        "api": settings.api_prefix,
        "docs": "/docs",
        "stream": f"{settings.api_prefix}/stream",
    }


@app.get("/healthz", tags=["system"])
async def healthz() -> dict[str, str]:
    """Liveness only. The substantive check is `GET {api_prefix}/health`."""
    return {"status": "ok"}
