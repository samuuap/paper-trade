"""Job bookkeeping.

Every scheduled unit of work — an ingest pass, a decision cycle, a metrics
rollup — is wrapped so that it leaves a `job_runs` row whether it succeeds or
fails. That table is what "job success rate" on the health panel is computed
from, and it is the first place to look when the system has been running
unattended overnight.

The row is written in its **own** session, deliberately. A job that fails rolls
its own transaction back; if its trace lived in that transaction it would roll
back too, and the failures — the only ones worth having — would be exactly the
ones that vanish.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from app.core.events import bus
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import JobRun, Phase, SystemEvent

log = get_logger(__name__)
UTC = dt.timezone.utc


@dataclass
class JobRecord:
    """Handle given to the job body so it can annotate its own outcome."""

    job: str
    phase: Optional[Phase] = None
    id: Optional[uuid.UUID] = None
    #: Anything the job wants kept: rows written, symbols touched, skips.
    details: dict[str, Any] = field(default_factory=dict)

    def note(self, **values: Any) -> None:
        self.details.update(values)


@asynccontextmanager
async def record_job(
    job: str, phase: Optional[Phase] = None, now: Optional[dt.datetime] = None
) -> AsyncIterator[JobRecord]:
    started_at = now or dt.datetime.now(UTC)
    record = JobRecord(job=job, phase=phase)

    async with session_scope() as session:
        row = JobRun(job=job, phase=phase, started_at=started_at)
        session.add(row)
        await session.flush()
        record.id = row.id

    bus.publish("job", job=job, status="started", phase=phase.value if phase else None)
    clock = time.perf_counter()
    error: Optional[str] = None
    try:
        yield record
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = (time.perf_counter() - clock) * 1000
        # Under a replay clock the simulated instant does not advance while the
        # job runs, so the elapsed wall time is added on to keep the row honest.
        finished_at = (
            started_at + dt.timedelta(milliseconds=duration_ms)
            if now is not None
            else dt.datetime.now(UTC)
        )
        try:
            async with session_scope() as session:
                row = await session.get(JobRun, record.id)
                if row is not None:
                    row.finished_at = finished_at
                    row.duration_ms = round(duration_ms, 2)
                    row.success = error is None
                    row.error = error
                    row.details = record.details
        except Exception:  # noqa: BLE001 - bookkeeping must never mask the job
            log.exception("job.record_failed", job=job)

        # `details` is job-supplied, so it is nested rather than splatted: a key
        # called "job" or "error" would otherwise collide with the fields here.
        log_fn = log.error if error else log.info
        log_fn(
            "job.finished",
            job=job,
            phase=phase.value if phase else None,
            duration_ms=round(duration_ms),
            success=error is None,
            error=error,
            details=record.details,
        )
        bus.publish(
            "job",
            job=job,
            status="failed" if error else "ok",
            phase=phase.value if phase else None,
            duration_ms=round(duration_ms, 1),
            error=error,
            level="error" if error else "info",
            details=record.details,
        )


async def system_event(
    category: str,
    event: str,
    *,
    message: str = "",
    level: str = "info",
    ts: Optional[dt.datetime] = None,
    **payload: Any,
) -> None:
    """Persist a noteworthy moment (phase change, degradation, recovery).

    Own session for the same reason `record_job` uses one: these are most
    valuable precisely when the surrounding transaction is about to fail.
    """
    try:
        async with session_scope() as session:
            session.add(
                SystemEvent(
                    ts=ts or dt.datetime.now(UTC),
                    level=level,
                    category=category,
                    event=event,
                    message=message,
                    payload=payload,
                )
            )
    except Exception:  # noqa: BLE001
        log.exception("system_event.persist_failed", category=category, event=event)
    bus.publish("system", category=category, event=event, message=message, level=level, **payload)
