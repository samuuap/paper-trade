"""Deterministic pre-trade risk and position sizing.

The model proposes; this module disposes. Every order is sized, clamped and
validated in ordinary Python before it can reach the ledger, which means a
confused LLM can produce a bad *choice* but never an illegal *trade*. Each
verdict carries structured notes so the dashboard can show exactly why an order
was shrunk or refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from app.core.config import settings
from app.db.models import OrderSide, OrderType
from app.engine.ledger import (
    ZERO,
    PositionState,
    apply_slippage,
    max_affordable_quantity,
    q_money,
    q_qty,
)


@dataclass
class RiskLimits:
    """Per-agent risk policy.

    Defaults come from settings, but each agent can override them through
    `agents.config["risk"]`. That is not a nicety: a 20% position cap is
    sensible for a stock picker and completely wrong for the Buy & Hold SPY
    baseline, which has to be 100% invested in one instrument for its return to
    track the index — which is the entire point of having it.
    """

    max_position_pct: float = 0.20
    max_gross_exposure_pct: float = 1.0
    min_order_notional: float = 100.0
    max_orders_per_cycle: int = 5
    cash_buffer_pct: float = 0.02
    allow_short: bool = False
    allow_fractional_shares: bool = True

    @classmethod
    def from_settings(cls) -> "RiskLimits":
        return cls(
            max_position_pct=settings.max_position_pct,
            max_gross_exposure_pct=settings.max_gross_exposure_pct,
            min_order_notional=settings.min_order_notional,
            max_orders_per_cycle=settings.max_orders_per_cycle,
            cash_buffer_pct=settings.cash_buffer_pct,
            allow_short=settings.allow_short,
            allow_fractional_shares=settings.allow_fractional_shares,
        )

    @classmethod
    def for_agent(cls, config: Optional[dict[str, Any]]) -> "RiskLimits":
        limits = cls.from_settings()
        overrides = (config or {}).get("risk") or {}
        for key, value in overrides.items():
            if hasattr(limits, key) and value is not None:
                setattr(limits, key, type(getattr(limits, key))(value))
        return limits


@dataclass
class OrderIntent:
    """What an agent asked for, before sizing.

    Exactly one of `quantity`, `notional` or `weight` needs to be set; when none
    is, `conviction` scales the maximum position size, which is the form the
    LLM decision schema uses.
    """

    symbol: str
    side: OrderSide
    quantity: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    weight: Optional[Decimal] = None
    conviction: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    rationale: str = ""


@dataclass
class RiskContext:
    equity: Decimal
    cash: Decimal
    positions: dict[str, PositionState]
    prices: dict[str, Decimal]
    tradable_symbols: Optional[set[str]] = None
    limits: RiskLimits = field(default_factory=RiskLimits.from_settings)


@dataclass
class RiskVerdict:
    approved: bool
    quantity: Decimal = ZERO
    reason: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return not self.approved


def _round_quantity(raw: Decimal, limits: RiskLimits) -> Decimal:
    if limits.allow_fractional_shares:
        # Four decimals is what brokers that support fractional trading use.
        return q_qty(Decimal(raw).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
    return Decimal(int(raw))


def target_quantity(intent: OrderIntent, context: RiskContext, price: Decimal) -> Decimal:
    """Translate whatever the agent expressed into a share count."""
    limits = context.limits
    if intent.quantity is not None and intent.quantity > 0:
        return _round_quantity(Decimal(intent.quantity), limits)

    if intent.notional is not None and intent.notional > 0:
        return _round_quantity(Decimal(intent.notional) / price, limits)

    if intent.weight is not None and intent.weight > 0:
        return _round_quantity(Decimal(intent.weight) * context.equity / price, limits)

    if intent.side is OrderSide.SELL:
        # A sell with no size means "close the position" — and closing means
        # closing. Rounding the holding down to the trading precision leaves a
        # sliver behind on every exit, and those slivers never go away: the
        # position count, the concentration index and the exposure figures all
        # end up counting dust as holdings. You can always sell exactly what
        # you own, so sell exactly that.
        existing = context.positions.get(intent.symbol)
        if existing and existing.quantity > 0:
            return q_qty(existing.quantity)
        return ZERO

    # Conviction scales the position cap: full conviction uses the whole
    # allowance, half conviction uses half of it.
    conviction = intent.conviction if intent.conviction is not None else 0.5
    conviction = max(0.0, min(1.0, float(conviction)))
    weight = Decimal(str(limits.max_position_pct)) * Decimal(str(conviction))
    return _round_quantity(weight * context.equity / price, limits)


def evaluate(intent: OrderIntent, context: RiskContext) -> RiskVerdict:
    symbol = intent.symbol.upper()
    notes: dict[str, Any] = {"symbol": symbol, "side": intent.side.value}

    if context.tradable_symbols is not None and symbol not in context.tradable_symbols:
        return RiskVerdict(False, reason=f"{symbol} is not in the tradable universe", notes=notes)

    price = context.prices.get(symbol)
    if price is None or price <= ZERO:
        return RiskVerdict(False, reason=f"no reference price available for {symbol}", notes=notes)
    notes["reference_price"] = float(price)

    if intent.order_type is OrderType.LIMIT and (
        intent.limit_price is None or intent.limit_price <= ZERO
    ):
        return RiskVerdict(False, reason="limit order without a valid limit price", notes=notes)

    quantity = target_quantity(intent, context, price)
    notes["requested_quantity"] = float(quantity)
    if quantity <= ZERO:
        return RiskVerdict(False, reason="sizing resolved to zero shares", notes=notes)

    existing = context.positions.get(symbol, PositionState())

    if intent.side is OrderSide.SELL:
        return _evaluate_sell(intent, context, symbol, price, quantity, existing, notes)
    return _evaluate_buy(intent, context, symbol, price, quantity, existing, notes)


def _evaluate_sell(
    intent: OrderIntent,
    context: RiskContext,
    symbol: str,
    price: Decimal,
    quantity: Decimal,
    existing: PositionState,
    notes: dict[str, Any],
) -> RiskVerdict:
    limits = context.limits
    available = existing.quantity if existing.quantity > ZERO else ZERO
    notes["position_quantity"] = float(existing.quantity)

    if not limits.allow_short:
        if available <= ZERO:
            return RiskVerdict(
                False, reason=f"no long position in {symbol} to sell (shorting disabled)", notes=notes
            )
        if quantity > available:
            notes["clamped_from"] = float(quantity)
            notes["clamp_reason"] = "shorting disabled"
            quantity = available

    notional = q_money(quantity * price)
    notes["notional"] = float(notional)
    # A close-out is always allowed through, even if it is a small notional:
    # blocking an exit on a minimum-size rule would be a genuinely dangerous rule.
    is_full_close = available > ZERO and quantity >= available
    if not is_full_close and notional < Decimal(str(limits.min_order_notional)):
        return RiskVerdict(
            False,
            reason=f"notional {notional:.2f} below minimum {limits.min_order_notional:.2f}",
            notes=notes,
        )

    return RiskVerdict(True, quantity=quantity, notes=notes)


def _evaluate_buy(
    intent: OrderIntent,
    context: RiskContext,
    symbol: str,
    price: Decimal,
    quantity: Decimal,
    existing: PositionState,
    notes: dict[str, Any],
) -> RiskVerdict:
    limits = context.limits
    equity = context.equity if context.equity > ZERO else Decimal("1")

    # --- concentration cap on the resulting position
    max_position_value = Decimal(str(limits.max_position_pct)) * equity
    existing_value = existing.market_value(price) if existing.quantity > ZERO else ZERO
    headroom_value = max_position_value - existing_value
    notes["position_cap_value"] = float(max_position_value)
    notes["existing_position_value"] = float(existing_value)
    if headroom_value <= ZERO:
        return RiskVerdict(
            False,
            reason=f"{symbol} already at the {limits.max_position_pct:.0%} position cap",
            notes=notes,
        )
    max_by_position = _round_quantity(headroom_value / price, limits)
    if quantity > max_by_position:
        notes["clamped_from"] = float(quantity)
        notes["clamp_reason"] = "position cap"
        quantity = max_by_position

    # --- gross exposure cap across the whole book
    gross = sum(
        (abs(state.market_value(context.prices.get(sym, state.avg_entry_price)))
         for sym, state in context.positions.items()
         if not state.is_flat),
        ZERO,
    )
    gross_cap = Decimal(str(limits.max_gross_exposure_pct)) * equity
    notes["gross_exposure"] = float(gross)
    notes["gross_exposure_cap"] = float(gross_cap)
    gross_headroom = gross_cap - gross
    if gross_headroom <= ZERO:
        return RiskVerdict(False, reason="gross exposure cap reached", notes=notes)
    max_by_gross = _round_quantity(gross_headroom / price, limits)
    if quantity > max_by_gross:
        notes["clamped_from"] = notes.get("clamped_from", float(quantity))
        notes["clamp_reason"] = "gross exposure cap"
        quantity = max_by_gross

    # --- cash, keeping a buffer so fees never push the account negative
    spendable = context.cash - Decimal(str(limits.cash_buffer_pct)) * equity
    notes["spendable_cash"] = float(spendable)
    if spendable <= ZERO:
        return RiskVerdict(False, reason="no spendable cash after the reserve buffer", notes=notes)

    # Affordability is checked against the price we expect to *pay*, not the
    # reference price. Sizing at the reference price and filling a few basis
    # points above it makes any order that spends the whole account fail at the
    # ledger — which is exactly what the Buy & Hold baseline always does, and it
    # is the one agent that has to work for the system to be trustworthy.
    expected_fill = apply_slippage(price, True, Decimal(str(settings.slippage_bps)))
    notes["expected_fill_price"] = float(expected_fill)

    affordable = max_affordable_quantity(
        cash=spendable,
        price=expected_fill,
        per_share=Decimal(str(settings.commission_per_share)),
        minimum_commission=Decimal(str(settings.commission_minimum)),
        maximum_pct=Decimal(str(settings.commission_maximum_pct)),
        fractional=limits.allow_fractional_shares,
    )
    if affordable <= ZERO:
        return RiskVerdict(False, reason="insufficient cash for even one share", notes=notes)
    if quantity > affordable:
        notes["clamped_from"] = notes.get("clamped_from", float(quantity))
        notes["clamp_reason"] = "insufficient cash"
        quantity = affordable

    if quantity <= ZERO:
        return RiskVerdict(False, reason="sizing clamped to zero", notes=notes)

    notional = q_money(quantity * price)
    notes["notional"] = float(notional)
    if notional < Decimal(str(limits.min_order_notional)):
        return RiskVerdict(
            False,
            reason=f"notional {notional:.2f} below minimum {limits.min_order_notional:.2f}",
            notes=notes,
        )

    notes["approved_quantity"] = float(quantity)
    return RiskVerdict(True, quantity=quantity, notes=notes)
