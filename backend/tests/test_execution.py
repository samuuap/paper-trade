"""Integration tests for the execution engine.

The ledger maths is unit-tested in `test_ledger.py`; what is checked here is
the part that only shows up once a database, a price store and a risk context
are involved: that cash, positions, fills and orders all move together and stay
consistent, and that every fill can be justified after the fact.

A bug in this layer does not raise — it quietly skews every metric downstream —
so the assertions are about conservation (cash in equals cash out) rather than
about the code doing what it happens to do today.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.base import session_scope
from app.db.models import Agent, Fill, Order, OrderSide, OrderStatus, OrderType, Phase, Portfolio
from app.engine.execution import ExecutionEngine
from app.engine.risk import OrderIntent, RiskLimits
from app.market import repository

D = Decimal
pytestmark = pytest.mark.usefixtures("roster")


async def _pair(session, slug: str = "momentum") -> tuple[Agent, Portfolio]:
    agent = (
        await session.execute(select(Agent).where(Agent.slug == slug))
    ).scalar_one()
    portfolio = (
        await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
    ).scalar_one()
    return agent, portfolio


def _buy(symbol="AAPL", **kwargs) -> OrderIntent:
    return OrderIntent(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, **kwargs)


def _sell(symbol="AAPL", **kwargs) -> OrderIntent:
    return OrderIntent(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET, **kwargs)


# ----------------------------------------------------------------- filling


async def test_a_buy_moves_cash_position_and_fees_together():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["AAPL"])
        cash_before = D(portfolio.cash)

        result = await engine.submit(agent, portfolio, _buy(weight=D("0.1")), context)

        assert result.filled
        fill = result.fills[0]
        positions = await engine.load_positions(portfolio.id)

        assert positions["AAPL"].quantity == fill.quantity
        assert D(portfolio.cash) == cash_before + D(fill.cash_delta)
        assert D(portfolio.fees_paid) == D(fill.commission)
        # cash_delta is the gross plus the fee, and it leaves the account.
        assert D(fill.cash_delta) == -(D(fill.gross_amount) + D(fill.commission))


async def test_slippage_works_against_the_buyer_and_is_recorded():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["AAPL"])

        result = await engine.submit(agent, portfolio, _buy(weight=D("0.05")), context)
        fill = result.fills[0]

        assert fill.price > fill.reference_price
        assert fill.slippage_bps == pytest.approx(settings.slippage_bps)
        assert D(fill.slippage_cost) == pytest.approx(
            (D(fill.price) - D(fill.reference_price)) * D(fill.quantity), abs=D("0.01")
        )


async def test_every_fill_is_traceable_to_the_quote_that_caused_it():
    """Auditability is the point of running our own ledger."""
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["MSFT"])

        result = await engine.submit(agent, portfolio, _buy("MSFT", weight=D("0.05")), context)
        fill = result.fills[0]

        reference = await repository.reference_price(session, "MSFT")
        assert reference is not None
        assert D(fill.reference_price) == reference[0]
        assert fill.reference_quote_ts is not None


async def test_a_rejected_order_is_persisted_with_its_reason_and_notes():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["AAPL"])

        result = await engine.submit(agent, portfolio, _sell(quantity=D("10")), context)

        assert not result.filled
        assert result.order.status is OrderStatus.REJECTED
        assert "shorting disabled" in result.order.reject_reason
        # The notes are what the dashboard shows when explaining a refusal.
        assert result.order.risk_notes["symbol"] == "AAPL"
        assert D(portfolio.cash) == D(portfolio.initial_capital)


async def test_an_unknown_symbol_is_rejected_rather_than_filled_at_a_guess():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["ZZZZ"])

        result = await engine.submit(agent, portfolio, _buy("ZZZZ", weight=D("0.1")), context)
        assert not result.filled
        assert "no reference price" in result.order.reject_reason


# -------------------------------------------------------------- round trip


async def test_a_round_trip_conserves_cash_against_realised_pnl_and_fees():
    """The invariant the whole P&L attribution rests on."""
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        start_cash = D(portfolio.cash)

        context = await engine.build_context(portfolio, extra_symbols=["NVDA"])
        opened = await engine.submit(agent, portfolio, _buy("NVDA", weight=D("0.1")), context)
        assert opened.filled

        context = await engine.build_context(portfolio, extra_symbols=["NVDA"])
        closed = await engine.submit(agent, portfolio, _sell("NVDA"), context)
        assert closed.filled

        fills = [opened.fills[0], closed.fills[0]]
        realised = sum(D(f.realized_pnl) for f in fills)
        fees = sum(D(f.commission) for f in fills)

        # Back to flat, so the cash move is exactly the P&L less the fees.
        assert (await engine.load_positions(portfolio.id)).get("NVDA") is None
        assert D(portfolio.cash) == pytest.approx(start_cash + realised - fees, abs=D("0.01"))
        assert D(portfolio.realized_pnl) == pytest.approx(realised, abs=D("0.000001"))
        assert D(portfolio.fees_paid) == pytest.approx(fees, abs=D("0.000001"))


async def test_scaling_in_reweights_the_stored_average_entry():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)

        # Sized by notional, not by share count: the synthetic price for any
        # given symbol is not a number this test should care about, and a
        # fixed quantity would sooner or later collide with the position cap.
        context = await engine.build_context(portfolio, extra_symbols=["KO"])
        first = await engine.submit(agent, portfolio, _buy("KO", notional=D("4000")), context)
        context = await engine.build_context(portfolio, extra_symbols=["KO"])
        second = await engine.submit(agent, portfolio, _buy("KO", notional=D("12000")), context)

        assert first.filled and second.filled
        q1, q2 = D(first.fills[0].quantity), D(second.fills[0].quantity)
        position = (await engine.load_positions(portfolio.id))["KO"]
        expected = (D(first.fills[0].price) * q1 + D(second.fills[0].price) * q2) / (q1 + q2)

        assert position.quantity == q1 + q2
        assert D(position.avg_entry_price) == pytest.approx(expected, abs=D("0.000001"))


# ----------------------------------------------------------------- batches


async def test_submit_many_stops_at_the_per_cycle_order_limit():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        limits = RiskLimits.from_settings()
        limits.max_orders_per_cycle = 2
        symbols = ["AAPL", "MSFT", "NVDA", "KO"]
        context = await engine.build_context(portfolio, extra_symbols=symbols, limits=limits)

        results = await engine.submit_many(
            agent,
            portfolio,
            [_buy(s, weight=D("0.05")) for s in symbols],
            context,
        )

        assert len(results) == 2
        orders = (
            await session.execute(select(Order).where(Order.agent_id == agent.id))
        ).scalars().all()
        assert len(orders) == 2


async def test_a_batch_cannot_overdraw_the_account():
    """Each order is sized against the cash the previous one actually left."""
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        limits = RiskLimits.from_settings()
        limits.max_position_pct = 1.0
        limits.max_orders_per_cycle = 5
        symbols = ["AAPL", "MSFT", "NVDA", "KO", "WMT"]
        context = await engine.build_context(portfolio, extra_symbols=symbols, limits=limits)

        await engine.submit_many(
            agent, portfolio, [_buy(s, weight=D("0.5")) for s in symbols], context
        )

        assert D(portfolio.cash) >= 0


# --------------------------------------------------------------- snapshots


async def test_the_equity_snapshot_matches_the_live_valuation():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["JNJ"])
        await engine.submit(agent, portfolio, _buy("JNJ", weight=D("0.1")), context)

        snapshot = await engine.snapshot_equity(agent, portfolio, Phase.OPEN)
        valuation = await engine.valuation(portfolio)

        assert D(snapshot.equity) == valuation.equity
        assert D(snapshot.cash) == valuation.cash
        assert snapshot.position_count == 1
        assert snapshot.phase is Phase.OPEN


async def test_fills_are_linked_to_the_order_and_the_agent():
    async with session_scope() as session:
        agent, portfolio = await _pair(session)
        engine = ExecutionEngine(session)
        context = await engine.build_context(portfolio, extra_symbols=["XOM"])
        result = await engine.submit(agent, portfolio, _buy("XOM", weight=D("0.05")), context)

        stored = (
            await session.execute(select(Fill).where(Fill.order_id == result.order.id))
        ).scalars().all()
        assert len(stored) == 1
        assert stored[0].agent_id == agent.id
        assert stored[0].portfolio_id == portfolio.id
