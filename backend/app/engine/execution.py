"""The execution engine: orders in, simulated fills out.

No external broker. Every fill is simulated against a real quote plus an
explicit slippage and commission model, and each `Fill` row stores the
reference price and quote timestamp that produced it — so any trade in the
system can be justified after the fact.

Running our own ledger is what makes six independent portfolios practical (no
paper-trading provider lets you open six accounts comfortably), and it means
the same code path serves live trading and historical replay.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.events import bus
from app.core.logging import get_logger
from app.db.models import (
    Agent,
    EquitySnapshot,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Phase,
    Portfolio,
    Position,
)
from app.engine.ledger import (
    DUST,
    ZERO,
    PortfolioValuation,
    PositionState,
    apply_fill,
    apply_slippage,
    commission_for,
    infer_position_state,
    q_money,
    q_qty,
    value_portfolio,
)
from app.engine.risk import OrderIntent, RiskContext, RiskLimits, RiskVerdict, evaluate
from app.market import repository

log = get_logger(__name__)
UTC = dt.timezone.utc


@dataclass
class ExecutionResult:
    order: Order
    verdict: RiskVerdict
    fills: list[Fill] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)

    def summary(self) -> dict:
        return {
            "order_id": str(self.order.id),
            "symbol": self.order.symbol,
            "side": self.order.side.value,
            "status": self.order.status.value,
            "quantity": float(self.order.quantity),
            "filled_quantity": float(self.order.filled_quantity),
            "avg_fill_price": float(self.order.avg_fill_price)
            if self.order.avg_fill_price
            else None,
            "reject_reason": self.order.reject_reason,
        }


class ExecutionEngine:
    """Stateless per-call; holds only the session and the replay cursor."""

    def __init__(self, session: AsyncSession, as_of: Optional[dt.datetime] = None) -> None:
        self.session = session
        #: In replay this pins every price lookup and fill timestamp to the
        #: simulated moment, so a replayed day is bit-for-bit reproducible.
        self.as_of = as_of

    @property
    def now(self) -> dt.datetime:
        return self.as_of or dt.datetime.now(UTC)

    # ------------------------------------------------------------- context
    async def load_positions(self, portfolio_id: uuid.UUID) -> dict[str, Position]:
        rows = (
            await self.session.execute(
                select(Position).where(Position.portfolio_id == portfolio_id)
            )
        ).scalars().all()
        # `DUST`, not zero: a residue smaller than the trading precision is not
        # a holding, and counting it as one inflates the position count and the
        # concentration index for the rest of the run.
        return {row.symbol: row for row in rows if abs(Decimal(row.quantity)) >= DUST}

    async def build_context(
        self,
        portfolio: Portfolio,
        extra_symbols: Iterable[str] = (),
        tradable_symbols: Optional[set[str]] = None,
        limits: Optional[RiskLimits] = None,
    ) -> RiskContext:
        positions = await self.load_positions(portfolio.id)
        states = {
            symbol: infer_position_state(row.quantity, row.avg_entry_price, row.realized_pnl)
            for symbol, row in positions.items()
        }
        symbols = set(states) | {s.upper() for s in extra_symbols}
        prices = await repository.reference_prices(self.session, sorted(symbols), as_of=self.as_of)
        valuation = value_portfolio(Decimal(portfolio.cash), states, prices)
        return RiskContext(
            equity=valuation.equity,
            cash=Decimal(portfolio.cash),
            positions=states,
            prices=prices,
            tradable_symbols=tradable_symbols,
            limits=limits or RiskLimits.from_settings(),
        )

    async def valuation(self, portfolio: Portfolio) -> PortfolioValuation:
        positions = await self.load_positions(portfolio.id)
        states = {
            symbol: infer_position_state(row.quantity, row.avg_entry_price, row.realized_pnl)
            for symbol, row in positions.items()
        }
        prices = await repository.reference_prices(
            self.session, sorted(states), as_of=self.as_of
        )
        return value_portfolio(Decimal(portfolio.cash), states, prices)

    # --------------------------------------------------------------- submit
    async def submit(
        self,
        agent: Agent,
        portfolio: Portfolio,
        intent: OrderIntent,
        context: RiskContext,
        decision_id: Optional[uuid.UUID] = None,
    ) -> ExecutionResult:
        """Size, validate, record and (if approved) fill a single order."""
        intent.symbol = intent.symbol.upper()
        verdict = evaluate(intent, context)

        order = Order(
            portfolio_id=portfolio.id,
            agent_id=agent.id,
            decision_id=decision_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=verdict.quantity if verdict.approved else (intent.quantity or q_qty(Decimal("0.0001"))),
            limit_price=intent.limit_price,
            status=OrderStatus.PENDING,
            rationale=intent.rationale,
            conviction=intent.conviction,
            risk_notes=verdict.notes,
            created_at=self.now,
            updated_at=self.now,
        )

        if not verdict.approved:
            order.status = OrderStatus.REJECTED
            order.reject_reason = verdict.reason
            self.session.add(order)
            await self.session.flush()
            log.info(
                "order.rejected",
                agent=agent.slug,
                symbol=intent.symbol,
                side=intent.side.value,
                reason=verdict.reason,
            )
            bus.publish(
                "order",
                agent=agent.slug,
                agent_id=str(agent.id),
                symbol=order.symbol,
                side=order.side.value,
                status=order.status.value,
                reason=verdict.reason,
            )
            return ExecutionResult(order=order, verdict=verdict)

        self.session.add(order)
        await self.session.flush()

        fills = await self._fill(order, agent, portfolio, context)
        result = ExecutionResult(order=order, verdict=verdict, fills=fills)

        bus.publish(
            "order",
            agent=agent.slug,
            agent_id=str(agent.id),
            **result.summary(),
        )
        return result

    async def submit_many(
        self,
        agent: Agent,
        portfolio: Portfolio,
        intents: Sequence[OrderIntent],
        context: RiskContext,
        decision_id: Optional[uuid.UUID] = None,
    ) -> list[ExecutionResult]:
        """Submit a batch, refreshing the risk context after each fill.

        Sequential on purpose: two orders sized against the same stale cash
        balance is exactly how a simulated account goes overdrawn.
        """
        results: list[ExecutionResult] = []
        for intent in intents[: context.limits.max_orders_per_cycle]:
            result = await self.submit(agent, portfolio, intent, context, decision_id)
            results.append(result)
            if result.filled:
                await self.session.refresh(portfolio)
                context = await self.build_context(
                    portfolio,
                    extra_symbols=[i.symbol for i in intents],
                    tradable_symbols=context.tradable_symbols,
                    limits=context.limits,
                )
        return results

    # ----------------------------------------------------------------- fill
    async def _fill(
        self,
        order: Order,
        agent: Agent,
        portfolio: Portfolio,
        context: RiskContext,
    ) -> list[Fill]:
        reference = await repository.reference_price(
            self.session, order.symbol, as_of=self.as_of
        )
        if reference is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "no reference price at execution time"
            order.updated_at = self.now
            return []

        reference_price, quote_ts, _source = reference
        is_buy = order.side is OrderSide.BUY
        fill_price = apply_slippage(
            reference_price, is_buy, Decimal(str(settings.slippage_bps))
        )

        # Limit orders only trade if the market is at or through the limit.
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            limit = Decimal(order.limit_price)
            if is_buy and reference_price > limit:
                order.status = OrderStatus.PENDING
                order.reject_reason = None
                order.updated_at = self.now
                return []
            if not is_buy and reference_price < limit:
                order.status = OrderStatus.PENDING
                order.updated_at = self.now
                return []
            # Never fill through the limit price in the client's favour.
            fill_price = min(fill_price, limit) if is_buy else max(fill_price, limit)

        quantity = await self._cap_by_liquidity(order.symbol, Decimal(order.quantity))
        if quantity <= ZERO:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "no liquidity available to fill against"
            order.updated_at = self.now
            return []

        commission = commission_for(
            quantity,
            fill_price,
            Decimal(str(settings.commission_per_share)),
            Decimal(str(settings.commission_minimum)),
            Decimal(str(settings.commission_maximum_pct)),
        )
        gross = q_money(quantity * fill_price)

        # Last-line defence: the risk layer already sized this, but the ledger
        # must never be allowed to go cash-negative on a rounding edge.
        if is_buy and gross + commission > Decimal(portfolio.cash):
            order.status = OrderStatus.REJECTED
            order.reject_reason = (
                f"insufficient cash at fill time: need {gross + commission:.2f}, "
                f"have {Decimal(portfolio.cash):.2f}"
            )
            order.updated_at = self.now
            return []

        signed = quantity if is_buy else -quantity
        previous_state = context.positions.get(order.symbol, PositionState())
        fill_result = apply_fill(previous_state, signed, fill_price)

        cash_delta = q_money(fill_result.trade_cash_delta - commission)
        portfolio.cash = q_money(Decimal(portfolio.cash) + cash_delta)
        portfolio.realized_pnl = q_money(
            Decimal(portfolio.realized_pnl) + fill_result.realized_pnl
        )
        portfolio.fees_paid = q_money(Decimal(portfolio.fees_paid) + commission)
        portfolio.updated_at = self.now

        await self._persist_position(portfolio.id, order.symbol, fill_result.position)

        slippage_cost = q_money(abs(fill_price - reference_price) * quantity)
        fill = Fill(
            order_id=order.id,
            portfolio_id=portfolio.id,
            agent_id=agent.id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=fill_price,
            reference_price=reference_price,
            reference_quote_ts=quote_ts,
            slippage_bps=float(settings.slippage_bps),
            slippage_cost=slippage_cost,
            commission=commission,
            gross_amount=gross,
            cash_delta=cash_delta,
            realized_pnl=fill_result.realized_pnl,
            ts=self.now,
        )
        self.session.add(fill)

        order.filled_quantity = quantity
        order.avg_fill_price = fill_price
        order.status = (
            OrderStatus.FILLED
            if quantity >= Decimal(order.quantity)
            else OrderStatus.PARTIALLY_FILLED
        )
        order.updated_at = self.now
        await self.session.flush()

        # Keep the in-memory context consistent for the rest of the batch.
        context.positions[order.symbol] = fill_result.position
        context.cash = Decimal(portfolio.cash)

        log.info(
            "order.filled",
            agent=agent.slug,
            symbol=order.symbol,
            side=order.side.value,
            quantity=float(quantity),
            price=float(fill_price),
            commission=float(commission),
            realized_pnl=float(fill_result.realized_pnl),
        )
        bus.publish(
            "fill",
            agent=agent.slug,
            agent_id=str(agent.id),
            symbol=order.symbol,
            side=order.side.value,
            quantity=float(quantity),
            price=float(fill_price),
            realized_pnl=float(fill_result.realized_pnl),
        )
        return [fill]

    async def _cap_by_liquidity(self, symbol: str, quantity: Decimal) -> Decimal:
        """Clamp an order to a plausible share of traded volume.

        Only binds for thin names — which is precisely when an unclamped
        simulation would be telling you comfortable lies.
        """
        rate = Decimal(str(settings.max_participation_rate))
        if rate <= ZERO:
            return quantity
        bars = await repository.get_bars(self.session, symbol, limit=1, as_of=self.as_of)
        if not bars or not bars[-1].volume:
            return quantity
        cap = q_qty(Decimal(bars[-1].volume) * rate)
        if cap <= ZERO:
            return quantity
        return min(quantity, cap)

    async def _persist_position(
        self, portfolio_id: uuid.UUID, symbol: str, state: PositionState
    ) -> None:
        row = (
            await self.session.execute(
                select(Position).where(
                    Position.portfolio_id == portfolio_id, Position.symbol == symbol
                )
            )
        ).scalars().first()

        if state.is_flat:
            if row is not None:
                # Keep the row so realised P&L history survives a round trip,
                # but zero the exposure.
                row.quantity = ZERO
                row.avg_entry_price = ZERO
                row.realized_pnl = state.realized_pnl
                row.updated_at = self.now
            return

        if row is None:
            self.session.add(
                Position(
                    portfolio_id=portfolio_id,
                    symbol=symbol,
                    quantity=state.quantity,
                    avg_entry_price=state.avg_entry_price,
                    realized_pnl=state.realized_pnl,
                    opened_at=self.now,
                    updated_at=self.now,
                )
            )
        else:
            row.quantity = state.quantity
            row.avg_entry_price = state.avg_entry_price
            row.realized_pnl = state.realized_pnl
            row.updated_at = self.now

    # ------------------------------------------------------------ snapshots
    async def snapshot_equity(
        self, agent: Agent, portfolio: Portfolio, phase: Optional[Phase] = None
    ) -> EquitySnapshot:
        valuation = await self.valuation(portfolio)
        snapshot = EquitySnapshot(
            agent_id=agent.id,
            ts=self.now.replace(microsecond=0),
            equity=valuation.equity,
            cash=valuation.cash,
            positions_value=valuation.positions_value,
            unrealized_pnl=valuation.unrealized_pnl,
            realized_pnl=Decimal(portfolio.realized_pnl),
            gross_exposure=valuation.gross_exposure,
            net_exposure=valuation.net_exposure,
            position_count=valuation.position_count,
            phase=phase,
        )
        await self.session.merge(snapshot)
        return snapshot
