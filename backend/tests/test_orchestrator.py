"""Tests for the orchestration layer.

Three things are being checked, in increasing order of integration:

1. The resource guard makes the right call and the cycle honours it.
2. A cycle isolates its agents from one another — the property that makes the
   competition fair when one of them misbehaves.
3. A whole simulated trading day runs end to end under a replay clock, with no
   network and no model. This is verification level 2 from PLAN.md: it
   exercises the phase machine, ingest, execution and the rollup together,
   without waiting for the market to open.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.base import session_scope
from app.db.models import (
    Agent,
    AgentKind,
    Decision,
    DecisionKind,
    DecisionStatus,
    EquitySnapshot,
    JobRun,
    Order,
    Phase,
    SystemEvent,
)
from app.market.calendar import ReplayClock, market_calendar
from app.orchestrator import cycle, ingest, resources
from app.orchestrator.jobs import record_job
from app.orchestrator.scheduler import Orchestrator

UTC = dt.timezone.utc


async def _deactivate_llm_agents() -> None:
    """Leave only the baselines, so a test needs no inference server."""
    async with session_scope() as session:
        await session.execute(
            update(Agent).where(Agent.kind == AgentKind.LLM).values(is_active=False)
        )


# ------------------------------------------------------- the resource guard


def test_market_hours_always_use_the_small_model():
    for phase in (Phase.PRE_MARKET, Phase.OPEN, Phase.AFTER_HOURS):
        assert resources.model_for_phase(phase) == settings.llm_model_open


def test_the_large_model_is_refused_when_it_cannot_fit(monkeypatch):
    monkeypatch.setattr(
        resources,
        "memory",
        lambda: resources.MemorySnapshot(total_gb=8.0, available_gb=6.0, used_pct=25.0),
    )
    assert resources.model_for_phase(Phase.CLOSED) == settings.llm_model_open


def test_the_large_model_is_chosen_when_there_is_room(monkeypatch):
    monkeypatch.setattr(
        resources,
        "memory",
        lambda: resources.MemorySnapshot(total_gb=36.0, available_gb=30.0, used_pct=16.0),
    )
    monkeypatch.setattr(resources, "inference_process", lambda: None)
    assert resources.model_for_phase(Phase.CLOSED) == settings.llm_model_closed


def test_memory_already_held_by_the_server_counts_towards_the_swap(monkeypatch):
    """Without this the loaded model vetoes its own replacement and flaps."""
    monkeypatch.setattr(
        resources,
        "memory",
        lambda: resources.MemorySnapshot(total_gb=36.0, available_gb=12.0, used_pct=66.0),
    )
    monkeypatch.setattr(
        resources,
        "inference_process",
        lambda: resources.InferenceProcess(pid=1, name="llama-server", rss_gb=13.0),
    )
    assert resources.model_for_phase(Phase.CLOSED) == settings.llm_model_closed


def test_inference_is_vetoed_below_the_memory_floor(monkeypatch):
    monkeypatch.setattr(
        resources,
        "memory",
        lambda: resources.MemorySnapshot(total_gb=36.0, available_gb=1.0, used_pct=97.0),
    )
    verdict = resources.check_inference()
    assert not verdict.allowed
    assert "below the" in verdict.reason


# -------------------------------------------------------------- the cycle


@pytest.mark.usefixtures("roster")
async def test_a_vetoed_cycle_records_skips_rather_than_silence(monkeypatch):
    """A missing row and a deliberate skip must not look the same."""
    monkeypatch.setattr(
        resources,
        "check_inference",
        lambda: resources.ResourceVerdict(
            allowed=False,
            reason="only 1.0 GB free",
            memory=resources.MemorySnapshot(36.0, 1.0, 97.0),
        ),
    )
    result = await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN)

    skipped = [o for o in result.outcomes if o.status == "skipped"]
    assert len(skipped) == 4  # the four LLM agents; baselines need no model

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Decision).where(Decision.status == DecisionStatus.SKIPPED)
            )
        ).scalars().all()
    assert len(rows) == 4
    assert all(r.error for r in rows)


@pytest.mark.usefixtures("roster")
async def test_one_failing_agent_does_not_sink_the_others(monkeypatch):
    original = cycle.build_runtime

    def exploding(agent):
        if agent.slug == "quant_sma":
            raise RuntimeError("boom")
        return original(agent)

    monkeypatch.setattr(cycle, "build_runtime", exploding)
    await _deactivate_llm_agents()

    result = await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN)
    statuses = {o.slug: o.status for o in result.outcomes}

    assert statuses["quant_sma"] == "error"
    assert statuses["buy_and_hold_spy"] == "ok"
    # The healthy agent's work survived the other's rollback.
    async with session_scope() as session:
        orders = (await session.execute(select(func.count(Order.id)))).scalar()
    assert orders >= 1


@pytest.mark.usefixtures("roster")
async def test_only_trade_cycles_can_move_the_ledger():
    """PLAN, REVIEW and RESEARCH produce narrative, never orders."""
    await _deactivate_llm_agents()
    await cycle.run_cycle(DecisionKind.REVIEW, Phase.AFTER_HOURS)

    async with session_scope() as session:
        orders = (await session.execute(select(func.count(Order.id)))).scalar()
    assert orders == 0


@pytest.mark.usefixtures("roster")
async def test_snapshotting_marks_every_portfolio():
    written = await cycle.snapshot_all_equity(Phase.OPEN)
    assert written == 6
    async with session_scope() as session:
        count = (await session.execute(select(func.count(EquitySnapshot.agent_id)))).scalar()
    assert count == 6


# ---------------------------------------------------------------- the jobs


async def test_a_job_leaves_a_trace_when_it_succeeds():
    async with record_job("test.ok") as record:
        record.note(rows=3)

    async with session_scope() as session:
        row = (
            await session.execute(select(JobRun).where(JobRun.job == "test.ok"))
        ).scalar_one()
    assert row.success is True
    assert row.details == {"rows": 3}
    assert row.duration_ms is not None


async def test_a_failing_job_still_leaves_its_trace():
    """The failures are the ones worth recording, so they get their own session."""
    with pytest.raises(ValueError):
        async with record_job("test.boom"):
            raise ValueError("nope")

    async with session_scope() as session:
        row = (
            await session.execute(select(JobRun).where(JobRun.job == "test.boom"))
        ).scalar_one()
    assert row.success is False
    assert "nope" in row.error


async def test_the_live_feed_set_stays_within_the_websocket_cap(market):
    async with session_scope() as session:
        symbols = await ingest.live_feed_symbols(session)
    assert "SPY" in symbols
    assert len(symbols) <= settings.finnhub_ws_max_symbols


# ------------------------------------------------------- a replayed day


@pytest.mark.usefixtures("roster")
async def test_a_full_trading_day_replays_offline(monkeypatch):
    """Verification level 2: the phase machine driven by a simulated clock.

    Runs pre-market through to the close with the baselines only, so it needs
    neither a network nor an inference server, and asserts that the day left
    the artefacts a real session would: decisions, orders, equity snapshots and
    a phase transition log.
    """
    await _deactivate_llm_agents()

    day = dt.date(2026, 3, 2)
    session_window = market_calendar.session_for(day)
    assert session_window is not None, "expected a trading session on the chosen day"

    clock = ReplayClock(
        start=session_window.pre_open - dt.timedelta(minutes=1),
        end=session_window.market_close + dt.timedelta(minutes=5),
        speed=1.0,
    )
    orchestrator = Orchestrator(clock=clock)

    # The universe and the bars are already in place from the fixture, and a
    # cold-start ingest here would pull in 500 symbols the test does not need.
    async def _skip_bootstrap() -> None:
        return None

    monkeypatch.setattr(orchestrator, "bootstrap", _skip_bootstrap)

    # Coarser cadence than production: a 60-second tick over a 16-hour window
    # would be ~950 iterations of work that proves nothing extra.
    for task in orchestrator.tasks:
        task.interval = dt.timedelta(hours=1)

    await orchestrator._run()

    assert clock.finished

    async with session_scope() as session:
        phases = {
            r.payload.get("phase")
            for r in (
                await session.execute(
                    select(SystemEvent).where(SystemEvent.category == "phase")
                )
            ).scalars().all()
        }
        jobs = set(
            (await session.execute(select(JobRun.job).where(JobRun.success.is_(True))))
            .scalars()
            .all()
        )
        decisions = (
            await session.execute(select(Decision).order_by(Decision.created_at))
        ).scalars().all()
        snapshots = (
            await session.execute(select(func.count(EquitySnapshot.agent_id)))
        ).scalar()
        orders = (await session.execute(select(func.count(Order.id)))).scalar()

    # It walked the day: pre-market, the session itself, then the close.
    assert {"PRE_MARKET", "OPEN", "AFTER_HOURS"} <= phases
    # Each phase asked for its own kind of turn, and the close ran the
    # after-the-bell pipeline. Asserted on the jobs rather than on decisions
    # because the baselines deliberately sit out every phase but OPEN — so an
    # empty PLAN cycle is correct behaviour, not a missing one.
    assert {
        "cycle.plan",
        "cycle.trade",
        "cycle.review",
        "market.daily_bars",
        "market.signals",
    } <= jobs
    assert DecisionKind.TRADE in {d.kind for d in decisions}
    assert orders >= 1
    assert snapshots >= 1
    # Every decision is pinned inside the replayed window, not to wall time.
    assert all(d.created_at.replace(tzinfo=UTC) <= clock.end for d in decisions)
