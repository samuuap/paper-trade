"""Tests for the metrics layer.

Split the same way the code is: hand-computed arithmetic for `financial`, and
database-backed checks for the rollup. The rollup tests care about scope — a
daily-scoped column holding an inception-to-date number, or the reverse, is a
bug that makes the leaderboard subtly and permanently wrong without ever
throwing.

The last test in this file is the one that matters most: Buy & Hold SPY has to
track the index. If it stops, the bug is in the engine or in the metrics, and
every other number in the system is suspect.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import (
    Agent,
    DecisionKind,
    EquitySnapshot,
    MarketBar,
    MarketQuote,
    Phase,
    Portfolio,
)
from app.engine.execution import ExecutionEngine
from app.metrics import financial as fin
from app.metrics import rollup
from app.orchestrator import cycle

D = Decimal
UTC = dt.timezone.utc


# ------------------------------------------------------- pure arithmetic


def test_daily_returns_are_period_over_period():
    assert fin.daily_returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])


def test_daily_returns_skip_non_positive_bases_instead_of_dividing_by_zero():
    assert fin.daily_returns([0.0, 100.0, 110.0]) == pytest.approx([0.1])


def test_cumulative_return_is_first_to_last():
    assert fin.cumulative_return([100.0, 80.0, 120.0]) == pytest.approx(0.2)


def test_max_drawdown_finds_the_worst_peak_to_trough():
    depth, days = fin.max_drawdown([100.0, 120.0, 90.0, 130.0])
    assert depth == pytest.approx(-0.25)
    assert days == 1


def test_max_drawdown_duration_runs_to_the_end_when_never_recovered():
    depth, days = fin.max_drawdown([100.0, 90.0, 80.0, 85.0])
    assert depth == pytest.approx(-0.2)
    assert days == 3


def test_current_drawdown_is_measured_against_the_running_peak():
    assert fin.current_drawdown([100.0, 150.0, 120.0]) == pytest.approx(-0.2)
    assert fin.current_drawdown([100.0, 150.0]) == pytest.approx(0.0)


def test_volatility_is_annualised_from_daily_observations():
    returns = [0.01, -0.01, 0.02, -0.02]
    # Sample stdev of the four = 0.018257...; annualised by sqrt(252).
    assert fin.volatility(returns) == pytest.approx(0.018257418 * (252**0.5), rel=1e-6)
    assert fin.volatility(returns, annualise=False) == pytest.approx(0.018257418, rel=1e-6)


def test_sharpe_is_mean_over_stdev_annualised():
    returns = [0.01, 0.02, 0.03]
    # mean 0.02, sample stdev 0.01 -> 2 * sqrt(252)
    assert fin.sharpe(returns) == pytest.approx(2.0 * (252**0.5), rel=1e-9)


def test_sortino_only_penalises_downside():
    upside_only = [0.01, 0.02, 0.03]
    mixed = [0.01, -0.02, 0.03]
    # With no losing days there is no downside deviation to divide by.
    assert fin.sortino(upside_only) is None
    assert fin.sortino(mixed) > fin.sharpe(mixed)


def test_var_95_is_the_fifth_percentile_and_reads_as_a_loss():
    returns = [-0.05, -0.02, 0.0, 0.01, 0.03]
    assert fin.var_95(returns) == pytest.approx(-0.044)


def test_beta_of_a_series_against_itself_is_one():
    returns = [0.01, -0.02, 0.03, 0.005]
    assert fin.beta(returns, returns) == pytest.approx(1.0)


def test_beta_of_a_doubled_series_is_two():
    benchmark = [0.01, -0.02, 0.03, 0.005]
    levered = [r * 2 for r in benchmark]
    assert fin.beta(levered, benchmark) == pytest.approx(2.0)


def test_alpha_is_zero_when_the_portfolio_is_the_benchmark():
    returns = [0.01, -0.02, 0.03, 0.005]
    assert fin.alpha(returns, returns) == pytest.approx(0.0, abs=1e-12)


def test_trade_statistics_are_computed_over_closed_trades():
    pnls = [100.0, -50.0, 200.0, 300.0]
    assert fin.win_rate(pnls) == pytest.approx(0.75)
    assert fin.profit_factor(pnls) == pytest.approx(600 / 50)
    assert fin.expectancy(pnls) == pytest.approx(137.5)


def test_profit_factor_is_undefined_rather_than_infinite_without_losses():
    assert fin.profit_factor([10.0, 20.0]) is None


def test_concentration_is_one_when_everything_is_in_a_single_name():
    assert fin.concentration_hhi([1.0]) == pytest.approx(1.0)
    assert fin.concentration_hhi([0.5, 0.5]) == pytest.approx(0.5)


def test_turnover_is_notional_over_average_equity():
    assert fin.turnover(50_000, 100_000) == pytest.approx(0.5)
    assert fin.turnover(50_000, 0) == 0.0


@pytest.mark.parametrize(
    "fn", [fin.sharpe, fin.sortino, fin.volatility, fin.var_95]
)
def test_ratios_return_none_rather_than_zero_without_enough_data(fn):
    """Zero and 'not enough history' are different statements."""
    assert fn([]) is None
    assert fn([0.01]) is None


def test_percentile_interpolates_between_observations():
    assert fin.percentile([0.0, 10.0], 50) == pytest.approx(5.0)
    assert fin.percentile([0.0, 10.0, 20.0], 50) == pytest.approx(10.0)
    assert fin.percentile([], 50) is None


# ------------------------------------------------------------- the rollup


async def _snapshot(session, agent_id, *, ts, equity, cash=D("0")):
    session.add(
        EquitySnapshot(
            agent_id=agent_id,
            ts=ts,
            equity=equity,
            cash=cash,
            positions_value=equity - cash,
            unrealized_pnl=D("0"),
            realized_pnl=D("0"),
            gross_exposure=equity - cash,
            net_exposure=equity - cash,
            position_count=1,
            phase=Phase.OPEN,
        )
    )


@pytest.mark.usefixtures("roster")
async def test_rollup_separates_daily_and_inception_to_date_scopes():
    tz = rollup.market_tz()
    day_one = dt.datetime(2026, 3, 2, 15, 0, tzinfo=tz)
    day_two = dt.datetime(2026, 3, 3, 15, 0, tzinfo=tz)

    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == "momentum"))
        ).scalar_one()
        await _snapshot(session, agent.id, ts=day_one, equity=D("100000"))
        await _snapshot(session, agent.id, ts=day_one + dt.timedelta(hours=1), equity=D("110000"))
        await _snapshot(session, agent.id, ts=day_two, equity=D("121000"))

    await rollup.rollup(days=3, through=day_two.date(), agent_slugs=["momentum"])

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(rollup.MetricsDaily).order_by(rollup.MetricsDaily.day)
            )
        ).scalars().all()

    assert [r.day for r in rows] == [day_one.date(), day_two.date()]

    first, second = rows
    # Daily scope: open and close are the first and last snapshot of that day.
    assert float(first.equity_open) == pytest.approx(100_000)
    assert float(first.equity_close) == pytest.approx(110_000)
    assert float(second.equity_open) == pytest.approx(121_000)

    # Day two's return is measured against day one's close, not its own open.
    assert second.daily_return == pytest.approx(0.1)
    # Inception-to-date scope: against the initial capital, not the day open.
    assert second.cumulative_return == pytest.approx(0.21)
    assert second.extras["observations"] == 2


@pytest.mark.usefixtures("roster")
async def test_rollup_is_idempotent():
    tz = rollup.market_tz()
    when = dt.datetime(2026, 4, 1, 15, 0, tzinfo=tz)
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == "value"))
        ).scalar_one()
        await _snapshot(session, agent.id, ts=when, equity=D("105000"))

    first = await rollup.rollup(days=1, through=when.date(), agent_slugs=["value"])
    second = await rollup.rollup(days=1, through=when.date(), agent_slugs=["value"])
    assert first == second == 1

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(rollup.MetricsDaily).where(rollup.MetricsDaily.agent_id == agent.id)
            )
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.usefixtures("roster")
async def test_the_benchmark_return_lines_up_with_the_days_it_describes():
    """Regression: daily bars are stamped at midnight UTC of their own date.

    Bucketing them by market timezone shifted every bar back a day, so the
    benchmark column never matched the day it sat on and silently rendered as
    a flat 0% for every agent.
    """
    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == "contrarian"))
        ).scalar_one()
        bars = (
            await session.execute(
                select(MarketBar)
                .where(MarketBar.symbol == "SPY", MarketBar.timeframe == "1D")
                .order_by(MarketBar.ts.desc())
                .limit(6)
            )
        ).scalars().all()
        bars = list(reversed(bars))
        assert len(bars) >= 3, "fixture should have ingested SPY history"

        for index, bar in enumerate(bars):
            day = rollup.bar_day(bar.ts)
            when = dt.datetime.combine(day, dt.time(15, 0), tzinfo=rollup.market_tz())
            await _snapshot(
                session, agent.id, ts=when, equity=D("100000") + D(index) * D("500")
            )

    first_day, last_day = rollup.bar_day(bars[0].ts), rollup.bar_day(bars[-1].ts)
    await rollup.rollup(
        days=(last_day - first_day).days + 1, through=last_day, agent_slugs=["contrarian"]
    )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(rollup.MetricsDaily)
                .where(rollup.MetricsDaily.agent_id == agent.id)
                .order_by(rollup.MetricsDaily.day)
            )
        ).scalars().all()

    assert rows, "the rollup produced no rows for the benchmark test"
    expected = float(bars[-1].close) / float(bars[0].close) - 1.0
    assert rows[-1].benchmark_return == pytest.approx(expected, abs=1e-9)
    # And it must actually move, rather than reading flat because every lookup missed.
    assert rows[-1].benchmark_return != 0.0


# --------------------------------------------------- the permanent alarm


@pytest.mark.usefixtures("roster")
async def test_buy_and_hold_tracks_the_benchmark():
    """The sanity check the whole system is measured against (see PLAN.md 4.7).

    Buy & Hold goes 100% into SPY, so once it is invested its return must move
    with SPY's, give or take the one-off entry cost. A divergence beyond that
    means the engine or the valuation is wrong, not the market.
    """
    async with session_scope() as session:
        await cycle.active_agents(session)

    await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN, only=["buy_and_hold_spy"])

    async with session_scope() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.slug == "buy_and_hold_spy"))
        ).scalar_one()
        portfolio = (
            await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
        ).scalar_one()
        engine = ExecutionEngine(session)

        before = await engine.valuation(portfolio)
        # Entry costs (slippage + commission) are paid once and are the only
        # legitimate gap between the agent and the index.
        entry_cost_pct = 1 - float(before.equity) / float(portfolio.initial_capital)
        assert 0 <= entry_cost_pct < 0.002

        quote = (
            await session.execute(
                select(MarketQuote)
                .where(MarketQuote.symbol == "SPY")
                .order_by(MarketQuote.ts.desc())
            )
        ).scalars().first()
        assert quote is not None

        # Move the index 10% and re-mark.
        session.add(
            MarketQuote(
                symbol="SPY",
                price=D(quote.price) * D("1.1"),
                prev_close=quote.price,
                ts=dt.datetime.now(UTC) + dt.timedelta(seconds=5),
                source="test",
            )
        )
        await session.flush()

        after = await engine.valuation(portfolio)

    agent_return = float(after.equity) / float(before.equity) - 1
    assert agent_return == pytest.approx(0.10, abs=0.0005)
