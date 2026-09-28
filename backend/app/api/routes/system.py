"""System health and operational controls.

The health payload is deliberately broad. This thing runs unattended on a
laptop, so "is it alive" is not a useful question — the useful questions are
whether data is still arriving, whether the model is still resident, whether
jobs are still succeeding, and whether the decision queue is backing up. Each
of those fails independently and each has its own section here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.core.events import bus
from app.db.models import JobRun, SystemEvent
from app.llm.client import get_client
from app.llm.queue import inference_queue
from app.metrics import queries
from app.orchestrator import ingest, resources
from app.orchestrator.scheduler import get_orchestrator

router = APIRouter(tags=["system"])
UTC = dt.timezone.utc


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Everything needed to tell a healthy system from a quietly broken one."""
    orchestrator = get_orchestrator()
    status = orchestrator.status()
    phase = orchestrator.phase

    jobs_window = dt.datetime.now(UTC) - dt.timedelta(hours=24)
    job_rows = (
        await db.execute(
            select(JobRun.job, JobRun.success, func.count(JobRun.id))
            .where(JobRun.started_at >= jobs_window)
            .group_by(JobRun.job, JobRun.success)
        )
    ).all()
    jobs: dict[str, dict[str, int]] = {}
    for job, success, count in job_rows:
        entry = jobs.setdefault(job, {"ok": 0, "failed": 0, "running": 0})
        key = "running" if success is None else ("ok" if success else "failed")
        entry[key] += int(count)

    total_ok = sum(j["ok"] for j in jobs.values())
    total_failed = sum(j["failed"] for j in jobs.values())

    return {
        "status": "ok",
        "env": settings.env,
        "orchestrator": status,
        "data": await queries.data_health(db),
        "provider": await ingest.provider_health(),
        "inference": resources.snapshot(phase),
        "decisions": await queries.decision_throughput(db),
        "jobs": {
            "window_hours": 24,
            "by_job": jobs,
            "success_rate": round(total_ok / (total_ok + total_failed), 4)
            if (total_ok + total_failed)
            else None,
        },
        "stream": {"subscribers": bus.subscriber_count},
    }


@router.get("/status")
async def orchestrator_status() -> dict[str, Any]:
    """Phase, session boundaries, next transition and per-task schedule."""
    return get_orchestrator().status()


@router.get("/jobs")
async def list_jobs(
    job: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(JobRun)
    if job:
        stmt = stmt.where(JobRun.job == job)
    rows = (
        await db.execute(stmt.order_by(desc(JobRun.started_at)).limit(limit))
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "job": r.job,
            "phase": r.phase.value if r.phase else None,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "duration_ms": r.duration_ms,
            "success": r.success,
            "error": r.error,
            "details": r.details,
        }
        for r in rows
    ]


@router.get("/events")
async def list_events(
    category: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(SystemEvent)
    if category:
        stmt = stmt.where(SystemEvent.category == category)
    if level:
        stmt = stmt.where(SystemEvent.level == level)
    rows = (
        await db.execute(stmt.order_by(desc(SystemEvent.ts)).limit(limit))
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "ts": r.ts.isoformat(),
            "level": r.level,
            "category": r.category,
            "event": r.event,
            "message": r.message,
            "payload": r.payload,
        }
        for r in rows
    ]


@router.get("/inference")
async def inference_health() -> dict[str, Any]:
    """Model residency, memory headroom, queue depth and server reachability."""
    orchestrator = get_orchestrator()
    snapshot = resources.snapshot(orchestrator.phase)
    server = await get_client().health()
    return {
        **snapshot,
        "queue": inference_queue.stats().to_dict(),
        "server": {**server, "structured_mode": settings.structured_mode},
    }
