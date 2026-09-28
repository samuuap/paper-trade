"""Building the `metrics_daily` rows.

One row per agent per day. Two different scopes live in that row, and mixing
them up is the easiest way to misread the leaderboard, so they are stated here
once and referenced everywhere else:

* **Daily-scoped** — `equity_open`, `equity_close`, `daily_return`, `turnover`,
  `trade_count`, the exposure and concentration figures, and every agent-economics
  column (decisions, tokens, inference seconds, shadow and energy cost). These
  describe *that day only*.
* **Inception-to-date** — `cumulative_return`, `realized_pnl`, `unrealized_pnl`,
  the ratios (Sharpe, Sortino, Calmar, volatility, VaR), the drawdown figures,
  the trade statistics (win rate, profit factor, expectancy) and
  `alpha`/`beta`/`benchmark_return`. These describe the whole run up to and
  including that day.

A "day" is the calendar date in the market timezone, not UTC. An overnight
snapshot at 02:00 ET belongs to the day that is about to start, which is what
you want: it is the opening equity of that session.

A "trade", for win rate and profit factor, is a **fill that realised P&L** —
that is, a closing or reducing fill. Scale-ins are not trades; the position
they build is counted when it is closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import (
    Agent,
    Decision,
    DecisionStatus,
    EquitySnapshot,
    Fill,
    MarketBar,
    MetricsDaily,
    Portfolio,
)
from app.db.upsert import upsert
from app.market.universe import BENCHMARK_SYMBOL
from app.metrics import financial as fin

log = get_logger(__name__)
UTC = dt.timezone.utc


def market_tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone_market)


def market_day(when: dt.datetime) -> dt.date:
    """The market-timezone calendar date a UTC instant belongs to."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(market_tz()).date()


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    tz = market_tz()
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz)
    end = start + dt.timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class DayBucket:
    """The snapshots of one market day, reduced to what a row needs."""

    first: EquitySnapshot
    last: EquitySnapshot
    equities: list[float] = field(default_factory=list)


async def _snapshots_by_day(
    session: AsyncSession, agent_id, start: dt.date, end: dt.date
) -> dict[dt.date, DayBucket]:
    lower, _ = _day_bounds(start)
    _, upper = _day_bounds(end)
    rows = (
        await session.execute(
            select(EquitySnapshot)
            .where(
                EquitySnapshot.agent_id == agent_id,
                EquitySnapshot.ts >= lower,
                EquitySnapshot.ts < upper,
            )
            .order_by(EquitySnapshot.ts)
        )
    ).scalars().all()

    buckets: dict[dt.date, DayBucket] = {}
    for row in rows:
        day = market_day(row.ts)
        bucket = buckets.get(day)
        if bucket is None:
            buckets[day] = DayBucket(first=row, last=row, equities=[float(row.equity)])
        else:
            bucket.last = row
            bucket.equities.append(float(row.equity))
    return buckets


async def _prior_closes(
    session: AsyncSession, agent_id, before: dt.date
) -> tuple[list[dt.date], list[float]]:
    """Daily closes already computed, so ratios expand over the whole run."""
    rows = (
        await session.execute(
            select(MetricsDaily.day, MetricsDaily.equity_close)
            .where(MetricsDaily.agent_id == agent_id, MetricsDaily.day < before)
            .order_by(MetricsDaily.day)
        )
    ).all()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def bar_day(ts: dt.datetime) -> dt.date:
    """The trading date a daily bar belongs to.

    Not `market_day`. A daily bar is stamped at midnight UTC *of its own
    trading date*, which in market time is the previous evening — so bucketing
    bars by market timezone shifts every one of them back a day, and the
    benchmark then never lines up with the equity curve it is supposed to be
    compared against.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).date()


async def _benchmark_closes(
    session: AsyncSession, start: dt.date, end: dt.date
) -> dict[dt.date, float]:
    # UTC bounds, for the same reason `bar_day` exists.
    lower = dt.datetime.combine(start, dt.time(0), tzinfo=UTC)
    upper = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=UTC)
    rows = (
        await session.execute(
            select(MarketBar.ts, MarketBar.close)
            .where(
                MarketBar.symbol == BENCHMARK_SYMBOL,
                MarketBar.timeframe == "1D",
                MarketBar.ts >= lower,
                MarketBar.ts < upper,
            )
            .order_by(MarketBar.ts)
        )
    ).all()
    return {bar_day(ts): float(close) for ts, close in rows}


async def _fills_for_day(session: AsyncSession, agent_id, day: dt.date) -> list[Fill]:
    lower, upper = _day_bounds(day)
    return list(
        (
            await session.execute(
                select(Fill).where(
                    Fill.agent_id == agent_id, Fill.ts >= lower, Fill.ts < upper
                )
            )
        ).scalars().all()
    )


async def _realised_trade_pnls(session: AsyncSession, agent_id, through: dt.date) -> list[float]:
    """Realised P&L of every closing fill up to the end of `through`."""
    _, upper = _day_bounds(through)
    rows = (
        await session.execute(
            select(Fill.realized_pnl).where(
                Fill.agent_id == agent_id, Fill.ts < upper, Fill.realized_pnl != 0
            )
        )
    ).scalars().all()
    return [float(v) for v in rows]


async def _decision_economics(session: AsyncSession, agent_id, day: dt.date) -> dict[str, Any]:
    lower, upper = _day_bounds(day)
    row = (
        await session.execute(
            select(
                func.count(Decision.id),
                func.coalesce(func.sum(Decision.prompt_tokens), 0),
                func.coalesce(func.sum(Decision.completion_tokens), 0),
                func.coalesce(func.sum(Decision.inference_ms), 0.0),
                func.coalesce(func.sum(Decision.shadow_cost_usd), 0.0),
                func.coalesce(func.sum(Decision.energy_cost_usd), 0.0),
                func.coalesce(func.sum(Decision.tool_call_count), 0),
                func.coalesce(func.sum(Decision.retries), 0),
            ).where(
                Decision.agent_id == agent_id,
                Decision.created_at >= lower,
                Decision.created_at < upper,
            )
        )
    ).one()
    failures = (
        await session.execute(
            select(func.count(Decision.id)).where(
                Decision.agent_id == agent_id,
                Decision.created_at >= lower,
                Decision.created_at < upper,
                Decision.status != DecisionStatus.OK,
            )
        )
    ).scalar() or 0
    return {
        "decisions": int(row[0] or 0),
        "prompt_tokens": int(row[1] or 0),
        "completion_tokens": int(row[2] or 0),
        "inference_seconds": round(float(row[3] or 0.0) / 1000, 3),
        "shadow_cost_usd": round(float(row[4] or 0.0), 6),
        "energy_cost_usd": round(float(row[5] or 0.0), 6),
        "tool_calls": int(row[6] or 0),
        "retries": int(row[7] or 0),
        "failed_decisions": int(failures),
    }


def _exposure_from(snapshot: EquitySnapshot) -> tuple[float, float]:
    equity = float(snapshot.equity) or 1.0
    return (
        float(snapshot.gross_exposure) / equity,
        float(snapshot.net_exposure) / equity,
    )


async def _position_weights(
    session: AsyncSession, portfolio_id, equity: float
) -> list[float]:
    from app.db.models import Position

    if equity <= 0:
        return []
    from app.engine.ledger import DUST

    rows = (
        await session.execute(select(Position).where(Position.portfolio_id == portfolio_id))
    ).scalars().all()
    return [
        abs(float(r.quantity) * float(r.avg_entry_price)) / equity
        for r in rows
        if abs(Decimal(r.quantity)) >= DUST
    ]


# --------------------------------------------------------------- the rollup


async def _contiguous_start(
    session: AsyncSession, agent_id, requested: dt.date
) -> dt.date:
    """Widen the window so the inception-to-date figures really are that.

    The expanding-window metrics (Sharpe, drawdown, cumulative return) are
    built from the daily closes already in `metrics_daily` plus the days being
    computed now. If the days before `requested` were never rolled up, that
    series starts mid-run and every ratio in the row silently describes a
    window instead of the whole thing — a max drawdown of -4% on a portfolio
    that is down 25% since inception, and no error anywhere.

    So: if there is no contiguous history immediately before `requested`, go
    back to where the history actually starts. One row per day makes this
    cheap even over a year.
    """
    last_known = (
        await session.execute(
            select(func.max(MetricsDaily.day)).where(
                MetricsDaily.agent_id == agent_id, MetricsDaily.day < requested
            )
        )
    ).scalar()
    if last_known is not None:
        return min(requested, last_known + dt.timedelta(days=1))

    first_snapshot = (
        await session.execute(
            select(func.min(EquitySnapshot.ts)).where(EquitySnapshot.agent_id == agent_id)
        )
    ).scalar()
    if first_snapshot is None:
        return requested
    return min(requested, market_day(first_snapshot))


async def rollup_agent(
    session: AsyncSession,
    agent: Agent,
    *,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    """Compute `metrics_daily` payloads for one agent over [start, end]."""
    start = await _contiguous_start(session, agent.id, start)
    buckets = await _snapshots_by_day(session, agent.id, start, end)
    if not buckets:
        return []

    portfolio = (
        await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
    ).scalar_one_or_none()
    initial_capital = float(portfolio.initial_capital) if portfolio else 0.0

    history_days, history_closes = await _prior_closes(session, agent.id, start)
    inception = history_days[0] if history_days else min(buckets)

    benchmark = await _benchmark_closes(session, inception, end)
    benchmark_base = next(
        (benchmark[d] for d in sorted(benchmark) if d >= inception), None
    )

    payloads: list[dict[str, Any]] = []
    closes = list(history_closes)
    days = list(history_days)
    #: Last benchmark close seen, carried across days the benchmark has no bar
    #: for (holidays, or a gap in ingest). Starts at the base so the first day
    #: reads as a flat 0% rather than as missing.
    benchmark_last = benchmark_base

    for day in sorted(buckets):
        bucket = buckets[day]
        equity_open = float(bucket.first.equity)
        equity_close = float(bucket.last.equity)

        previous_close = closes[-1] if closes else equity_open
        daily_return = (equity_close / previous_close - 1.0) if previous_close > 0 else 0.0

        closes.append(equity_close)
        days.append(day)
        returns = fin.daily_returns(closes)

        max_dd, max_dd_days = fin.max_drawdown(closes)
        cumulative = (
            equity_close / initial_capital - 1.0 if initial_capital > 0 else None
        )
        annualised = fin.annualised_return(cumulative, len(closes))

        # The benchmark leg is aligned on the days this agent actually has, so
        # a gap in either series cannot silently shift the pairing.
        benchmark_closes_aligned = [benchmark[d] for d in days if d in benchmark]
        benchmark_returns = fin.daily_returns(benchmark_closes_aligned)
        beta_value = fin.beta(returns, benchmark_returns)
        benchmark_last = benchmark.get(day, benchmark_last)
        benchmark_return = (
            benchmark_last / benchmark_base - 1.0
            if benchmark_base and benchmark_last
            else None
        )

        pnls = await _realised_trade_pnls(session, agent.id, day)
        fills = await _fills_for_day(session, agent.id, day)
        traded_notional = sum(float(f.gross_amount) for f in fills)
        average_equity = sum(bucket.equities) / len(bucket.equities)

        weights = await _position_weights(session, portfolio.id, equity_close) if portfolio else []
        gross_pct, net_pct = _exposure_from(bucket.last)
        economics = await _decision_economics(session, agent.id, day)

        payloads.append(
            {
                "agent_id": agent.id,
                "day": day,
                "equity_open": Decimal(str(round(equity_open, 6))),
                "equity_close": Decimal(str(round(equity_close, 6))),
                "daily_return": daily_return,
                "cumulative_return": cumulative if cumulative is not None else 0.0,
                "realized_pnl": Decimal(str(round(float(bucket.last.realized_pnl), 6))),
                "unrealized_pnl": Decimal(str(round(float(bucket.last.unrealized_pnl), 6))),
                "sharpe": fin.sharpe(returns),
                "sortino": fin.sortino(returns),
                "calmar": fin.calmar(annualised, max_dd),
                "volatility": fin.volatility(returns),
                "max_drawdown": max_dd,
                "max_drawdown_days": max_dd_days,
                "current_drawdown": fin.current_drawdown(closes),
                "var_95": fin.var_95(returns),
                "win_rate": fin.win_rate(pnls),
                "profit_factor": fin.profit_factor(pnls),
                "expectancy": fin.expectancy(pnls),
                "trade_count": len(fills),
                "turnover": fin.turnover(traded_notional, average_equity),
                "gross_exposure_pct": gross_pct,
                "net_exposure_pct": net_pct,
                "concentration_hhi": fin.concentration_hhi(weights),
                "alpha": fin.alpha(returns, benchmark_returns, beta_value),
                "beta": beta_value,
                "benchmark_return": benchmark_return,
                "decisions": economics["decisions"],
                "prompt_tokens": economics["prompt_tokens"],
                "completion_tokens": economics["completion_tokens"],
                "inference_seconds": economics["inference_seconds"],
                "shadow_cost_usd": economics["shadow_cost_usd"],
                "energy_cost_usd": economics["energy_cost_usd"],
                "extras": {
                    "observations": len(closes),
                    "snapshots": len(bucket.equities),
                    "benchmark_symbol": BENCHMARK_SYMBOL,
                    "traded_notional": round(traded_notional, 2),
                    "closed_trades": len(pnls),
                    "tool_calls": economics["tool_calls"],
                    "retries": economics["retries"],
                    "failed_decisions": economics["failed_decisions"],
                    "annualised_return": annualised,
                },
                "computed_at": dt.datetime.now(UTC),
            }
        )

    return payloads


async def rollup(
    *,
    days: int = 1,
    through: Optional[dt.date] = None,
    agent_slugs: Optional[Sequence[str]] = None,
) -> int:
    """Recompute the last `days` days. The scheduled after-close job."""
    end = through or market_day(dt.datetime.now(UTC))
    start = end - dt.timedelta(days=max(days, 1) - 1)

    written = 0
    async with session_scope() as session:
        stmt = select(Agent)
        if agent_slugs:
            stmt = stmt.where(Agent.slug.in_([s.lower() for s in agent_slugs]))
        agents = (await session.execute(stmt)).scalars().all()

        for agent in agents:
            payloads = await rollup_agent(session, agent, start=start, end=end)
            if not payloads:
                continue
            await upsert(
                session,
                MetricsDaily.__table__,
                payloads,
                index_elements=[MetricsDaily.agent_id, MetricsDaily.day],
                update_columns=[c for c in payloads[0] if c not in ("agent_id", "day")],
            )
            written += len(payloads)

    log.info("metrics.rollup", days=days, through=str(end), rows=written)
    return written


async def recompute_all(agent_slugs: Optional[Sequence[str]] = None) -> int:
    """Rebuild every row from the equity snapshots. Maintenance, not scheduled.

    Needed whenever a metric definition changes — which is the normal way this
    code evolves, so it is a first-class operation rather than a script someone
    writes in a hurry.
    """
    async with session_scope() as session:
        oldest = (await session.execute(select(func.min(EquitySnapshot.ts)))).scalar()
    if oldest is None:
        return 0
    start = market_day(oldest)
    end = market_day(dt.datetime.now(UTC))
    return await rollup(days=(end - start).days + 1, through=end, agent_slugs=agent_slugs)


__all__ = ["market_day", "recompute_all", "rollup", "rollup_agent"]
