"""The 24/7 phase machine.

The whole system's heartbeat. It asks the market calendar what phase it is,
notices when that answer changes, and runs the work each phase calls for:

| Phase         | On entering                              | Recurring                          |
|---------------|------------------------------------------|------------------------------------|
| `PRE_MARKET`  | news ingest, then one `PLAN` turn         | quotes, equity snapshots           |
| `OPEN`        | —                                         | `TRADE` cycles, quotes, snapshots, news |
| `AFTER_HOURS` | daily bars + signals, `REVIEW`, rollup    | quotes, equity snapshots           |
| `CLOSED`      | —                                         | `RESEARCH` turns                   |

Deliberate omissions: nothing polls quotes while the market is shut, because
the prices would not move and the request budget is finite; and no equity
snapshots are taken then either, for the same reason — a flat line costs a row
a minute all night and tells you nothing.

**Interruptions are normal, not exceptional.** This runs on a laptop that gets
closed. There is no in-memory state worth preserving: every task is idempotent
or additive, the phase is recomputed from the clock on every tick, and a
restart mid-session simply picks up from whatever the calendar says now. That
is also what lets the same loop be driven by a `ReplayClock` — the only
difference between live and replay is which clock is injected and whether
reads are pinned with `as_of`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select

from app.core.config import settings
from app.core.events import bus
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import DecisionKind, Phase, UniverseMember
from app.market.calendar import Clock, MarketCalendar, RealClock, ReplayClock, market_calendar
from app.metrics import rollup
from app.orchestrator import ingest
from app.orchestrator.cycle import run_cycle, snapshot_all_equity
from app.orchestrator.jobs import system_event

log = get_logger(__name__)
UTC = dt.timezone.utc

#: Phases in which the market data is actually moving.
_LIVE_PHASES = frozenset({Phase.PRE_MARKET, Phase.OPEN, Phase.AFTER_HOURS})

TaskFn = Callable[[dt.datetime, Phase], Awaitable[Any]]


@dataclass
class Task:
    name: str
    interval: dt.timedelta
    phases: frozenset[Phase]
    fn: TaskFn
    next_due: Optional[dt.datetime] = None
    last_run: Optional[dt.datetime] = None
    runs: int = 0
    failures: int = 0
    last_error: Optional[str] = None

    def active_in(self, phase: Phase) -> bool:
        return phase in self.phases

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_seconds": self.interval.total_seconds(),
            "phases": sorted(p.value for p in self.phases),
            "next_due": self.next_due.isoformat() if self.next_due else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "runs": self.runs,
            "failures": self.failures,
            "last_error": self.last_error,
        }


def build_clock() -> Clock:
    """Real clock, or a replay cursor when `replay_mode` is set."""
    if not settings.replay_mode:
        return RealClock()
    if not settings.replay_start or not settings.replay_end:
        raise ValueError("replay_mode needs both replay_start and replay_end")
    start = dt.datetime.fromisoformat(settings.replay_start)
    end = dt.datetime.fromisoformat(settings.replay_end)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return ReplayClock(start, end, speed=settings.replay_speed)


class Orchestrator:
    def __init__(
        self,
        clock: Optional[Clock] = None,
        calendar: Optional[MarketCalendar] = None,
    ) -> None:
        self.clock = clock or build_clock()
        self.calendar = calendar or market_calendar
        self.phase: Optional[Phase] = None
        self.started_at: Optional[dt.datetime] = None
        self.tick_count = 0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.tasks: list[Task] = self._build_tasks()
        self._on_enter: dict[Phase, list[tuple[str, TaskFn]]] = {
            Phase.PRE_MARKET: [
                ("premarket.news", self._ingest_news),
                ("premarket.plan", self._plan_cycle),
            ],
            Phase.AFTER_HOURS: [
                ("close.daily_ingest", self._daily_close),
                ("close.review", self._review_cycle),
                ("close.metrics", self._metrics_rollup),
            ],
        }

    # ------------------------------------------------------------- wiring
    def _build_tasks(self) -> list[Task]:
        return [
            Task(
                name="cycle.trade",
                interval=dt.timedelta(minutes=settings.decision_interval_minutes),
                phases=frozenset({Phase.OPEN}),
                fn=self._trade_cycle,
            ),
            Task(
                name="cycle.research",
                interval=dt.timedelta(hours=settings.research_interval_hours),
                phases=frozenset({Phase.CLOSED}),
                fn=self._research_cycle,
            ),
            Task(
                name="market.quotes",
                interval=dt.timedelta(seconds=settings.quote_poll_seconds),
                phases=_LIVE_PHASES,
                fn=self._ingest_quotes,
            ),
            Task(
                name="market.news",
                interval=dt.timedelta(minutes=settings.news_poll_minutes),
                phases=frozenset({Phase.OPEN}),
                fn=self._ingest_news,
            ),
            Task(
                name="portfolio.snapshot",
                interval=dt.timedelta(seconds=settings.equity_snapshot_seconds),
                phases=_LIVE_PHASES,
                fn=self._snapshot,
            ),
        ]

    def _as_of(self, now: dt.datetime) -> Optional[dt.datetime]:
        """Replay pins every read; live mode uses the real clock everywhere."""
        return now if self.clock.is_replay else None

    # -------------------------------------------------------------- units
    async def _trade_cycle(self, now: dt.datetime, phase: Phase) -> Any:
        return await run_cycle(DecisionKind.TRADE, phase, as_of=self._as_of(now))

    async def _plan_cycle(self, now: dt.datetime, phase: Phase) -> Any:
        return await run_cycle(DecisionKind.PLAN, phase, as_of=self._as_of(now))

    async def _review_cycle(self, now: dt.datetime, phase: Phase) -> Any:
        return await run_cycle(DecisionKind.REVIEW, phase, as_of=self._as_of(now))

    async def _research_cycle(self, now: dt.datetime, phase: Phase) -> Any:
        return await run_cycle(DecisionKind.RESEARCH, phase, as_of=self._as_of(now))

    async def _ingest_quotes(self, now: dt.datetime, phase: Phase) -> Any:
        return await ingest.ingest_quotes(phase=phase, now=self._as_of(now))

    async def _ingest_news(self, now: dt.datetime, phase: Phase) -> Any:
        return await ingest.ingest_news(phase=phase, now=self._as_of(now))

    async def _daily_close(self, now: dt.datetime, phase: Phase) -> Any:
        return await ingest.daily_close_ingest(phase=phase, now=self._as_of(now))

    async def _snapshot(self, now: dt.datetime, phase: Phase) -> Any:
        return await snapshot_all_equity(phase, as_of=self._as_of(now))

    async def _metrics_rollup(self, now: dt.datetime, phase: Phase) -> Any:
        # Two days, not one: a session that crosses midnight UTC, or a restart
        # that missed yesterday's run, would otherwise leave a hole.
        return await rollup.rollup(days=2, through=rollup.market_day(now))

    # ----------------------------------------------------------- lifecycle
    async def bootstrap(self) -> None:
        """Make the system usable from a cold database.

        Without this a fresh install has an empty universe and an empty signal
        cache, so every screen returns nothing and every agent is handed a
        blank candidate list — which looks like a model failure and is not.
        """
        await ingest.sync_universe_job(now=self._as_of(self.clock.now()))

        async with session_scope() as session:
            screenable = (
                await session.execute(
                    select(func.count())
                    .select_from(UniverseMember)
                    .where(UniverseMember.signals_updated_at.is_not(None))
                )
            ).scalar() or 0

        if screenable == 0:
            log.info("orchestrator.cold_start_ingest")
            await system_event(
                "orchestrator",
                "cold_start",
                message="No cached signals found; running a first bar ingest.",
            )
            now = self.clock.now()
            await ingest.daily_close_ingest(now=self._as_of(now))

    async def start(self) -> None:
        if self._task is not None:
            return
        self.started_at = dt.datetime.now(UTC)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="orchestrator")
        log.info(
            "orchestrator.started",
            replay=self.clock.is_replay,
            interval_minutes=settings.decision_interval_minutes,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("orchestrator.stopped", ticks=self.tick_count)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---------------------------------------------------------------- loop
    async def _run(self) -> None:
        try:
            await self.bootstrap()
        except Exception:  # noqa: BLE001 - a failed bootstrap must not stop the loop
            log.exception("orchestrator.bootstrap_failed")

        while not self._stop.is_set():
            self.tick_count += 1
            now = self.clock.now()
            phase = self.calendar.phase_at(now)

            if phase is not self.phase:
                await self._enter_phase(self.phase, phase, now)

            for task in self.tasks:
                if not task.active_in(phase):
                    continue
                if task.next_due is not None and task.next_due > now:
                    continue
                await self._run_task(task, now, phase)
                # Scheduled from the due time, not from completion, so a slow
                # cycle does not make the cadence drift later every time.
                base = task.next_due or now
                task.next_due = max(base + task.interval, now)

            wake = self._next_wake(now, phase)
            if self.clock.is_replay:
                if getattr(self.clock, "finished", False):
                    log.info("orchestrator.replay_finished", ticks=self.tick_count)
                    await system_event("orchestrator", "replay_finished", ticks=self.tick_count)
                    return
                self.clock.advance_to(wake)
            else:
                await self._sleep_until(now, wake)

    async def _run_task(self, task: Task, now: dt.datetime, phase: Phase) -> None:
        try:
            await task.fn(now, phase)
        except Exception as exc:  # noqa: BLE001 - one task must not stop the loop
            task.failures += 1
            task.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("orchestrator.task_failed", task=task.name, phase=phase.value)
            await system_event(
                "orchestrator",
                "task_failed",
                message=task.last_error,
                level="error",
                task=task.name,
                phase=phase.value,
            )
        else:
            task.last_error = None
        finally:
            task.runs += 1
            task.last_run = now

    async def _enter_phase(
        self, previous: Optional[Phase], phase: Phase, now: dt.datetime
    ) -> None:
        self.phase = phase
        log.info(
            "orchestrator.phase",
            previous=previous.value if previous else None,
            phase=phase.value,
            at=now.isoformat(),
        )
        await system_event(
            "phase",
            "transition",
            message=f"{previous.value if previous else 'start'} -> {phase.value}",
            ts=now,
            previous=previous.value if previous else None,
            phase=phase.value,
        )
        bus.publish(
            "phase",
            phase=phase.value,
            previous=previous.value if previous else None,
            at=now.isoformat(),
        )

        # Anything scheduled for this phase is due now. Tasks belonging to the
        # phase we just left are cleared so they do not fire stale on return.
        for task in self.tasks:
            task.next_due = now if task.active_in(phase) else None

        for name, hook in self._on_enter.get(phase, []):
            try:
                await hook(now, phase)
            except Exception:  # noqa: BLE001
                log.exception("orchestrator.hook_failed", hook=name, phase=phase.value)
                await system_event(
                    "orchestrator", "hook_failed", level="error", hook=name, phase=phase.value
                )

    def _next_wake(self, now: dt.datetime, phase: Phase) -> dt.datetime:
        candidates = [
            task.next_due
            for task in self.tasks
            if task.active_in(phase) and task.next_due is not None
        ]
        transition, _ = self.calendar.next_transition(now)
        candidates.append(transition)
        return min(candidates)

    async def _sleep_until(self, now: dt.datetime, wake: dt.datetime) -> None:
        delay = max((wake - now).total_seconds(), 0.0)
        delay = min(delay, settings.scheduler_max_sleep_seconds)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=max(delay, 0.05))

    # -------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        phase = self.phase or self.calendar.phase_at(now)
        session = self.calendar.session_containing(now) or self.calendar.next_session(now)
        transition_at, transition_to = self.calendar.next_transition(now)
        uptime = (
            (dt.datetime.now(UTC) - self.started_at).total_seconds() if self.started_at else 0.0
        )
        return {
            "running": self.running,
            "phase": phase.value,
            "now": now.isoformat(),
            "uptime_seconds": round(uptime, 1),
            "ticks": self.tick_count,
            "replay": self.clock.is_replay,
            "replay_cursor": now.isoformat() if self.clock.is_replay else None,
            "session": session.to_dict() if session else None,
            "next_transition": {
                "at": transition_at.isoformat(),
                "phase": transition_to.value,
            },
            "tasks": [t.to_dict() for t in self.tasks],
        }


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def set_orchestrator(instance: Optional[Orchestrator]) -> None:
    """Swap the singleton (tests, replay harnesses)."""
    global _orchestrator
    _orchestrator = instance


__all__ = [
    "Orchestrator",
    "Task",
    "build_clock",
    "get_orchestrator",
    "set_orchestrator",
]
