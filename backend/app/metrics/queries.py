"""Read-side aggregates.

Everything the dashboard asks for, shaped as plain JSON-ready dicts so the API
layer stays a thin translation of HTTP to a function call. Keeping the shaping
here rather than in the routes means the same numbers can be pulled from a
script or a notebook without going through the web server.

Where a figure exists in `metrics_daily` it is read from there rather than
recomputed: the rollup is the single definition of each metric, and two
implementations of "Sharpe" would eventually disagree.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    Agent,
    Decision,
    DecisionStatus,
    EquitySnapshot,
    Fill,
    MetricsDaily,
    Order,
    Portfolio,
    Position,
    UniverseMember,
)
from app.engine.execution import ExecutionEngine
from app.engine.ledger import DUST
from app.llm import pricing
from app.market import repository
from app.market.universe import BENCHMARK_SYMBOL
from app.metrics import financial as fin

UTC = dt.timezone.utc

#: How many recent decisions the latency percentiles are computed over. Bounded
#: so the health panel cannot turn into a full-table scan as history grows.
LATENCY_WINDOW = 500


def _f(value: Any, default: float = 0.0) -> float:
    return float(value) if value is not None else default


async def _latest_metrics(session: AsyncSession) -> dict[Any, MetricsDaily]:
    """Most recent `metrics_daily` row per agent."""
    newest = (
        select(MetricsDaily.agent_id, func.max(MetricsDaily.day).label("day"))
        .group_by(MetricsDaily.agent_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(MetricsDaily).join(
                newest,
                (MetricsDaily.agent_id == newest.c.agent_id)
                & (MetricsDaily.day == newest.c.day),
            )
        )
    ).scalars().all()
    return {row.agent_id: row for row in rows}


# ------------------------------------------------------------- leaderboard


async def leaderboard(session: AsyncSession) -> list[dict[str, Any]]:
    """Every agent, ranked by return, with the cost of getting there.

    Equity is marked to market live rather than read from the last daily row,
    so the ranking moves during the session instead of jumping at midnight.
    """
    agents = (
        await session.execute(select(Agent).where(Agent.is_active.is_(True)))
    ).scalars().all()
    metrics = await _latest_metrics(session)
    engine = ExecutionEngine(session)

    rows: list[dict[str, Any]] = []
    for agent in agents:
        portfolio = (
            await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
        ).scalar_one_or_none()
        if portfolio is None:
            continue

        valuation = await engine.valuation(portfolio)
        initial = float(portfolio.initial_capital)
        equity = float(valuation.equity)
        daily = metrics.get(agent.id)

        totals = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Decision.prompt_tokens), 0),
                    func.coalesce(func.sum(Decision.completion_tokens), 0),
                    func.coalesce(func.sum(Decision.inference_ms), 0.0),
                    func.coalesce(func.sum(Decision.shadow_cost_usd), 0.0),
                    func.coalesce(func.sum(Decision.energy_cost_usd), 0.0),
                    func.count(Decision.id),
                ).where(Decision.agent_id == agent.id)
            )
        ).one()
        inference_seconds = _f(totals[2]) / 1000
        pnl = equity - initial

        rows.append(
            {
                "agent_id": str(agent.id),
                "slug": agent.slug,
                "name": agent.name,
                "kind": agent.kind.value,
                "runtime": agent.runtime,
                "color": agent.color,
                "equity": round(equity, 2),
                "cash": round(float(valuation.cash), 2),
                "positions_value": round(float(valuation.positions_value), 2),
                "position_count": valuation.position_count,
                "initial_capital": round(initial, 2),
                "total_pnl": round(pnl, 2),
                "total_return_pct": round((equity / initial - 1) * 100, 4) if initial else 0.0,
                "realized_pnl": round(float(portfolio.realized_pnl), 2),
                "unrealized_pnl": round(float(valuation.unrealized_pnl), 2),
                "fees_paid": round(float(portfolio.fees_paid), 2),
                # From the rollup: one definition per metric, read not recomputed.
                "sharpe": daily.sharpe if daily else None,
                "sortino": daily.sortino if daily else None,
                "calmar": daily.calmar if daily else None,
                "volatility": daily.volatility if daily else None,
                "max_drawdown": daily.max_drawdown if daily else None,
                "current_drawdown": daily.current_drawdown if daily else None,
                "win_rate": daily.win_rate if daily else None,
                "profit_factor": daily.profit_factor if daily else None,
                "alpha": daily.alpha if daily else None,
                "beta": daily.beta if daily else None,
                "benchmark_return": daily.benchmark_return if daily else None,
                "metrics_day": daily.day.isoformat() if daily else None,
                "decisions": int(totals[5] or 0),
                "prompt_tokens": int(totals[0] or 0),
                "completion_tokens": int(totals[1] or 0),
                "total_tokens": int((totals[0] or 0) + (totals[1] or 0)),
                "inference_seconds": round(inference_seconds, 2),
                "shadow_cost_usd": round(_f(totals[3]), 4),
                "energy_cost_usd": round(_f(totals[4]), 6),
                "seconds_per_pnl_dollar": pricing.compute_seconds_per_pnl_dollar(
                    inference_seconds, pnl
                ),
            }
        )

    rows.sort(key=lambda r: r["total_return_pct"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


# ----------------------------------------------------------- equity curves


async def equity_curves(
    session: AsyncSession,
    *,
    since: Optional[dt.datetime] = None,
    slugs: Optional[Sequence[str]] = None,
    max_points: int = 1500,
) -> dict[str, Any]:
    """Equity series per agent, plus the benchmark rebased to the same start.

    Rebasing the benchmark onto each agent's initial capital is what makes the
    chart answer the only question that matters: did this beat just buying the
    index?
    """
    stmt = select(Agent).where(Agent.is_active.is_(True))
    if slugs:
        stmt = stmt.where(Agent.slug.in_([s.lower() for s in slugs]))
    agents = (await session.execute(stmt)).scalars().all()

    series: list[dict[str, Any]] = []
    for agent in agents:
        query = select(EquitySnapshot).where(EquitySnapshot.agent_id == agent.id)
        if since is not None:
            query = query.where(EquitySnapshot.ts >= since)
        rows = (
            await session.execute(query.order_by(EquitySnapshot.ts))
        ).scalars().all()

        # Even thinning, so the shape survives but the payload does not grow
        # without bound over months of minute-resolution snapshots.
        step = max(1, len(rows) // max_points) if max_points else 1
        thinned = rows[::step]
        if rows and thinned and thinned[-1] is not rows[-1]:
            thinned.append(rows[-1])

        series.append(
            {
                "slug": agent.slug,
                "name": agent.name,
                "color": agent.color,
                "kind": agent.kind.value,
                "points": [
                    {"ts": r.ts.isoformat(), "equity": round(float(r.equity), 2)}
                    for r in thinned
                ],
            }
        )

    # Clip the benchmark to the window the agents actually cover. The bar
    # history usually runs much further back than the first equity snapshot,
    # and an overlay that spans a year against curves that span a month
    # squashes every curve into the right-hand edge — the one thing the chart
    # exists to show becomes unreadable.
    earliest = _earliest_point(series)
    bounds = [_aware(b) for b in (since, earliest) if b is not None]
    floor = max(bounds) if bounds else None
    benchmark = await _benchmark_series(session, since=floor)
    return {"series": series, "benchmark": benchmark}


def _earliest_point(series: list[dict[str, Any]]) -> Optional[dt.datetime]:
    stamps = [
        _aware(dt.datetime.fromisoformat(entry["points"][0]["ts"]))
        for entry in series
        if entry["points"]
    ]
    return min(stamps) if stamps else None


async def _benchmark_series(
    session: AsyncSession, *, since: Optional[dt.datetime] = None
) -> dict[str, Any]:
    bars = await repository.get_bars(session, BENCHMARK_SYMBOL, limit=2000)
    if since is not None:
        # Daily bars are stamped at midnight UTC of their trading date, so the
        # bar covering the first snapshot's day starts before it. Reaching one
        # day back keeps that bar, which is the rebasing point.
        floor = since - dt.timedelta(days=1)
        bars = [b for b in bars if _aware(b.ts) >= floor]
    if not bars:
        return {"symbol": BENCHMARK_SYMBOL, "points": []}
    base = float(bars[0].close)
    if base <= 0:
        return {"symbol": BENCHMARK_SYMBOL, "points": []}
    capital = settings.initial_capital
    return {
        "symbol": BENCHMARK_SYMBOL,
        "points": [
            {
                "ts": b.ts.isoformat(),
                "equity": round(capital * float(b.close) / base, 2),
                "close": float(b.close),
            }
            for b in bars
        ],
    }


def _aware(value: dt.datetime) -> dt.datetime:
    """SQLite hands back naive datetimes; everything is stored as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ---------------------------------------------------------------- an agent


async def agent_detail(session: AsyncSession, slug: str) -> Optional[dict[str, Any]]:
    agent = (
        await session.execute(select(Agent).where(Agent.slug == slug.lower()))
    ).scalar_one_or_none()
    if agent is None:
        return None
    portfolio = (
        await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
    ).scalar_one_or_none()
    if portfolio is None:
        return None

    engine = ExecutionEngine(session)
    valuation = await engine.valuation(portfolio)
    # Same `DUST` rule the engine applies: rounding residue is not a holding.
    positions = [
        row
        for row in (
            await session.execute(
                select(Position).where(Position.portfolio_id == portfolio.id)
            )
        ).scalars().all()
        if abs(Decimal(row.quantity)) >= DUST
    ]
    prices = await repository.reference_prices(session, [p.symbol for p in positions])

    equity = float(valuation.equity) or 1.0
    holdings = []
    for row in positions:
        price = prices.get(row.symbol)
        mark = float(price) if price is not None else float(row.avg_entry_price)
        value = float(row.quantity) * mark
        cost = float(row.quantity) * float(row.avg_entry_price)
        holdings.append(
            {
                "symbol": row.symbol,
                "quantity": float(row.quantity),
                "avg_entry_price": float(row.avg_entry_price),
                "price": mark,
                "market_value": round(value, 2),
                "unrealized_pnl": round(value - cost, 2),
                "unrealized_pnl_pct": round((mark / float(row.avg_entry_price) - 1) * 100, 4)
                if row.avg_entry_price
                else 0.0,
                "weight_pct": round(value / equity * 100, 3),
                "opened_at": row.opened_at.isoformat(),
            }
        )
    holdings.sort(key=lambda h: h["market_value"], reverse=True)

    history = (
        await session.execute(
            select(MetricsDaily)
            .where(MetricsDaily.agent_id == agent.id)
            .order_by(MetricsDaily.day)
        )
    ).scalars().all()

    return {
        "agent": {
            "id": str(agent.id),
            "slug": agent.slug,
            "name": agent.name,
            "kind": agent.kind.value,
            "runtime": agent.runtime,
            "description": agent.description,
            "color": agent.color,
            "model": agent.model,
            "config": agent.config,
            "strategy_prompt": agent.strategy_prompt,
            "is_active": agent.is_active,
        },
        "portfolio": {
            "equity": round(float(valuation.equity), 2),
            "cash": round(float(valuation.cash), 2),
            "positions_value": round(float(valuation.positions_value), 2),
            "initial_capital": round(float(portfolio.initial_capital), 2),
            "realized_pnl": round(float(portfolio.realized_pnl), 2),
            "unrealized_pnl": round(float(valuation.unrealized_pnl), 2),
            "fees_paid": round(float(portfolio.fees_paid), 2),
            "gross_exposure": round(float(valuation.gross_exposure), 2),
            "net_exposure": round(float(valuation.net_exposure), 2),
        },
        "positions": holdings,
        "history": [_metrics_row(m) for m in history],
    }


def _metrics_row(row: MetricsDaily) -> dict[str, Any]:
    return {
        "day": row.day.isoformat(),
        "equity_open": float(row.equity_open),
        "equity_close": float(row.equity_close),
        "daily_return": row.daily_return,
        "cumulative_return": row.cumulative_return,
        "sharpe": row.sharpe,
        "sortino": row.sortino,
        "calmar": row.calmar,
        "volatility": row.volatility,
        "max_drawdown": row.max_drawdown,
        "current_drawdown": row.current_drawdown,
        "var_95": row.var_95,
        "win_rate": row.win_rate,
        "profit_factor": row.profit_factor,
        "expectancy": row.expectancy,
        "trade_count": row.trade_count,
        "turnover": row.turnover,
        "gross_exposure_pct": row.gross_exposure_pct,
        "net_exposure_pct": row.net_exposure_pct,
        "concentration_hhi": row.concentration_hhi,
        "alpha": row.alpha,
        "beta": row.beta,
        "benchmark_return": row.benchmark_return,
        "decisions": row.decisions,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "inference_seconds": row.inference_seconds,
        "shadow_cost_usd": row.shadow_cost_usd,
        "energy_cost_usd": row.energy_cost_usd,
        "extras": row.extras,
    }


# ------------------------------------------------------------- decisions


async def decisions(
    session: AsyncSession,
    *,
    slug: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    stmt = select(Decision, Agent).join(Agent, Agent.id == Decision.agent_id)
    if slug:
        stmt = stmt.where(Agent.slug == slug.lower())
    if kind:
        stmt = stmt.where(Decision.kind == kind.upper())
    if status:
        stmt = stmt.where(Decision.status == status.upper())
    stmt = stmt.order_by(desc(Decision.created_at)).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": str(d.id),
            "agent": a.slug,
            "agent_name": a.name,
            "color": a.color,
            "cycle_id": str(d.cycle_id),
            "phase": d.phase.value,
            "kind": d.kind.value,
            "status": d.status.value,
            "model": d.model,
            "market_view": d.market_view,
            "reasoning": d.reasoning,
            "output": d.output,
            "tool_calls": d.tool_calls,
            "prompt_tokens": d.prompt_tokens,
            "completion_tokens": d.completion_tokens,
            "total_tokens": d.total_tokens,
            "inference_ms": d.inference_ms,
            "queue_wait_ms": d.queue_wait_ms,
            "tokens_per_second": d.tokens_per_second,
            "tool_call_count": d.tool_call_count,
            "retries": d.retries,
            "shadow_cost_usd": d.shadow_cost_usd,
            "energy_cost_usd": d.energy_cost_usd,
            "error": d.error,
            "created_at": d.created_at.isoformat(),
        }
        for d, a in rows
    ]


# ----------------------------------------------------------------- trades


async def trade_log(
    session: AsyncSession,
    *,
    slug: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Every fill, with the order and the reasoning that produced it."""
    stmt = (
        select(Fill, Order, Agent)
        .join(Order, Order.id == Fill.order_id)
        .join(Agent, Agent.id == Fill.agent_id)
    )
    if slug:
        stmt = stmt.where(Agent.slug == slug.lower())
    if symbol:
        stmt = stmt.where(Fill.symbol == symbol.upper())
    stmt = stmt.order_by(desc(Fill.ts)).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": str(f.id),
            "ts": f.ts.isoformat(),
            "agent": a.slug,
            "agent_name": a.name,
            "color": a.color,
            "symbol": f.symbol,
            "side": f.side.value,
            "quantity": float(f.quantity),
            "price": float(f.price),
            "reference_price": float(f.reference_price),
            "slippage_bps": f.slippage_bps,
            "slippage_cost": float(f.slippage_cost),
            "commission": float(f.commission),
            "gross_amount": float(f.gross_amount),
            "cash_delta": float(f.cash_delta),
            "realized_pnl": float(f.realized_pnl),
            "order_id": str(o.id),
            "decision_id": str(o.decision_id) if o.decision_id else None,
            "rationale": o.rationale,
            "conviction": o.conviction,
        }
        for f, o, a in rows
    ]


async def rejected_orders(
    session: AsyncSession, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Orders the risk layer refused. The audit trail of "model proposes"."""
    rows = (
        await session.execute(
            select(Order, Agent)
            .join(Agent, Agent.id == Order.agent_id)
            .where(Order.status == "REJECTED")
            .order_by(desc(Order.created_at))
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": str(o.id),
            "ts": o.created_at.isoformat(),
            "agent": a.slug,
            "symbol": o.symbol,
            "side": o.side.value,
            "quantity": float(o.quantity),
            "reject_reason": o.reject_reason,
            "rationale": o.rationale,
            "risk_notes": o.risk_notes,
        }
        for o, a in rows
    ]


# -------------------------------------------------------- agent economics


async def agent_economics(session: AsyncSession) -> list[dict[str, Any]]:
    """Token spend, latency distribution and the two comparative costs."""
    agents = (await session.execute(select(Agent))).scalars().all()
    out: list[dict[str, Any]] = []

    for agent in agents:
        totals = (
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
                    func.coalesce(func.avg(Decision.tokens_per_second), 0.0),
                ).where(Decision.agent_id == agent.id)
            )
        ).one()
        count = int(totals[0] or 0)
        if count == 0:
            continue

        latencies = (
            await session.execute(
                select(Decision.inference_ms)
                .where(Decision.agent_id == agent.id, Decision.inference_ms > 0)
                .order_by(desc(Decision.created_at))
                .limit(LATENCY_WINDOW)
            )
        ).scalars().all()
        latencies = [float(v) for v in latencies]

        by_status = (
            await session.execute(
                select(Decision.status, func.count(Decision.id))
                .where(Decision.agent_id == agent.id)
                .group_by(Decision.status)
            )
        ).all()

        portfolio = (
            await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
        ).scalar_one_or_none()
        pnl = 0.0
        if portfolio is not None:
            engine = ExecutionEngine(session)
            valuation = await engine.valuation(portfolio)
            pnl = float(valuation.equity) - float(portfolio.initial_capital)

        inference_seconds = _f(totals[3]) / 1000
        out.append(
            {
                "slug": agent.slug,
                "name": agent.name,
                "kind": agent.kind.value,
                "color": agent.color,
                "decisions": count,
                "prompt_tokens": int(totals[1] or 0),
                "completion_tokens": int(totals[2] or 0),
                "total_tokens": int((totals[1] or 0) + (totals[2] or 0)),
                "tokens_per_decision": round(((totals[1] or 0) + (totals[2] or 0)) / count, 1),
                "inference_seconds": round(inference_seconds, 2),
                "avg_tokens_per_second": round(_f(totals[8]), 2),
                "tool_calls": int(totals[6] or 0),
                "retries": int(totals[7] or 0),
                "latency_ms": {
                    "p50": fin.percentile(latencies, 50),
                    "p95": fin.percentile(latencies, 95),
                    "p99": fin.percentile(latencies, 99),
                    "samples": len(latencies),
                },
                "status_counts": {str(status.value): int(n) for status, n in by_status},
                "shadow_cost_usd": round(_f(totals[4]), 4),
                "energy_cost_usd": round(_f(totals[5]), 6),
                "shadow_model": settings.shadow_model_name,
                "pnl": round(pnl, 2),
                "seconds_per_pnl_dollar": pricing.compute_seconds_per_pnl_dollar(
                    inference_seconds, pnl
                ),
            }
        )

    out.sort(key=lambda r: r["total_tokens"], reverse=True)
    return out


# ------------------------------------------------------------------ health


async def data_health(session: AsyncSession) -> dict[str, Any]:
    lag = await repository.data_lag_seconds(session)
    coverage = await repository.bar_coverage(session)
    universe_size = (
        await session.execute(
            select(func.count()).select_from(UniverseMember).where(
                UniverseMember.is_active.is_(True)
            )
        )
    ).scalar()
    # A member with no cached signals is invisible to the screener, so the gap
    # between these two numbers is the honest measure of screening coverage.
    screenable = (
        await session.execute(
            select(func.count()).select_from(UniverseMember).where(
                UniverseMember.is_active.is_(True),
                UniverseMember.signals_updated_at.is_not(None),
            )
        )
    ).scalar()
    return {
        "quote_lag_seconds": round(lag, 1) if lag is not None else None,
        "universe_size": int(universe_size or 0),
        "screenable_symbols": int(screenable or 0),
        **coverage,
    }


async def decision_throughput(session: AsyncSession, hours: int = 24) -> dict[str, Any]:
    since = dt.datetime.now(UTC) - dt.timedelta(hours=hours)
    total = (
        await session.execute(
            select(func.count(Decision.id)).where(Decision.created_at >= since)
        )
    ).scalar() or 0
    failed = (
        await session.execute(
            select(func.count(Decision.id)).where(
                Decision.created_at >= since, Decision.status != DecisionStatus.OK
            )
        )
    ).scalar() or 0
    return {
        "window_hours": hours,
        "decisions": int(total),
        "failed": int(failed),
        "per_hour": round(int(total) / hours, 2) if hours else 0.0,
        "success_rate": round(1 - int(failed) / int(total), 4) if total else None,
    }


__all__ = [
    "agent_detail",
    "agent_economics",
    "data_health",
    "decision_throughput",
    "decisions",
    "equity_curves",
    "leaderboard",
    "rejected_orders",
    "trade_log",
]
