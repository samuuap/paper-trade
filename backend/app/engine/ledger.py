"""Position accounting primitives.

Pure functions over `Decimal`, no database and no I/O, so every rule here can be
pinned down by a deterministic unit test. This is the piece where a subtle bug
would not raise anything — it would just quietly poison every metric in the
system — so it is kept small, total and boring.

Cost basis is **average cost**: buying into an existing position re-weights the
entry price, selling realises P&L against that average. Crossing through zero
(a sell larger than a long position, when shorting is enabled) closes the old
side, realises its P&L, and opens the remainder at the fill price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional

MONEY_DP = Decimal("0.000001")
QTY_DP = Decimal("0.000001")
ZERO = Decimal("0")

#: Smallest quantity that can be traded (four decimals, what brokers offering
#: fractional shares use). Anything left below it is residue from rounding, not
#: a position: it cannot be sold, it cannot be marked meaningfully, and left
#: alone it quietly inflates every exposure metric.
DUST = Decimal("0.0001")


def q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_DP, rounding=ROUND_HALF_UP)


def q_qty(value: Decimal) -> Decimal:
    return Decimal(value).quantize(QTY_DP, rounding=ROUND_HALF_UP)


class LedgerError(Exception):
    """Raised when an operation would corrupt the books."""


@dataclass(frozen=True)
class PositionState:
    """Signed position. Negative quantity is a short."""

    quantity: Decimal = ZERO
    avg_entry_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    @property
    def is_flat(self) -> bool:
        """Flat, including "flat but for rounding residue" — see `DUST`."""
        return abs(self.quantity) < DUST

    @property
    def is_long(self) -> bool:
        return self.quantity > ZERO

    @property
    def is_short(self) -> bool:
        return self.quantity < ZERO

    def market_value(self, price: Decimal) -> Decimal:
        return q_money(self.quantity * price)

    def cost_basis(self) -> Decimal:
        return q_money(abs(self.quantity) * self.avg_entry_price)

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        if self.is_flat:
            return ZERO
        return q_money((price - self.avg_entry_price) * self.quantity)


@dataclass(frozen=True)
class FillResult:
    position: PositionState
    realized_pnl: Decimal
    #: Cash movement from the trade itself, before fees. Negative on buys.
    trade_cash_delta: Decimal


def apply_fill(
    state: PositionState,
    signed_quantity: Decimal,
    price: Decimal,
) -> FillResult:
    """Apply a signed fill (+ buy, - sell) and return the new position state.

    Fees are handled by the caller: they affect cash and total P&L but not the
    cost basis, which keeps "did the trade work" separable from "what did it
    cost to trade".
    """
    if signed_quantity == ZERO:
        raise LedgerError("fill quantity must be non-zero")
    if price <= ZERO:
        raise LedgerError(f"fill price must be positive, got {price}")

    delta = q_qty(signed_quantity)
    price = Decimal(price)
    current = state.quantity
    trade_cash_delta = q_money(-delta * price)

    # Opening from flat.
    if current == ZERO:
        return FillResult(
            position=replace(state, quantity=delta, avg_entry_price=q_money(price)),
            realized_pnl=ZERO,
            trade_cash_delta=trade_cash_delta,
        )

    same_direction = (current > ZERO) == (delta > ZERO)

    # Adding to the existing side: re-weight the average entry, realise nothing.
    if same_direction:
        total_qty = current + delta
        weighted = abs(current) * state.avg_entry_price + abs(delta) * price
        new_avg = q_money(weighted / abs(total_qty))
        return FillResult(
            position=replace(state, quantity=q_qty(total_qty), avg_entry_price=new_avg),
            realized_pnl=ZERO,
            trade_cash_delta=trade_cash_delta,
        )

    # Reducing, closing, or flipping.
    closing_qty = min(abs(delta), abs(current))
    direction = Decimal(1) if current > ZERO else Decimal(-1)
    realized = q_money((price - state.avg_entry_price) * closing_qty * direction)
    remaining = q_qty(current + delta)

    if remaining == ZERO:
        new_position = PositionState(
            quantity=ZERO,
            avg_entry_price=ZERO,
            realized_pnl=q_money(state.realized_pnl + realized),
        )
    elif (remaining > ZERO) == (current > ZERO):
        # Partial reduction: the average entry of what remains is unchanged.
        new_position = PositionState(
            quantity=remaining,
            avg_entry_price=state.avg_entry_price,
            realized_pnl=q_money(state.realized_pnl + realized),
        )
    else:
        # Flipped through zero: the leftover opens a fresh position at the fill.
        new_position = PositionState(
            quantity=remaining,
            avg_entry_price=q_money(price),
            realized_pnl=q_money(state.realized_pnl + realized),
        )

    return FillResult(
        position=new_position,
        realized_pnl=realized,
        trade_cash_delta=trade_cash_delta,
    )


def commission_for(
    quantity: Decimal,
    price: Decimal,
    per_share: Decimal,
    minimum: Decimal,
    maximum_pct: Decimal,
) -> Decimal:
    """Per-share commission with a floor and a percentage-of-notional ceiling.

    The ceiling matters for cheap stocks: without it, a per-share fee on a
    $2 name is a multi-percent round-trip cost and the simulation stops
    resembling anything real.
    """
    quantity = abs(Decimal(quantity))
    if quantity == ZERO:
        return ZERO
    notional = quantity * Decimal(price)
    fee = max(quantity * Decimal(per_share), Decimal(minimum))
    ceiling = notional * Decimal(maximum_pct)
    return q_money(min(fee, ceiling))


def apply_slippage(
    reference_price: Decimal, is_buy: bool, slippage_bps: Decimal
) -> Decimal:
    """Slippage always works against you: buys fill higher, sells fill lower."""
    factor = Decimal(slippage_bps) / Decimal(10_000)
    adjusted = Decimal(reference_price) * (Decimal(1) + factor if is_buy else Decimal(1) - factor)
    return q_money(max(adjusted, Decimal("0.0001")))


@dataclass
class PortfolioValuation:
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    position_count: int

    def to_dict(self) -> dict:
        return {
            "cash": float(self.cash),
            "positions_value": float(self.positions_value),
            "equity": float(self.equity),
            "unrealized_pnl": float(self.unrealized_pnl),
            "gross_exposure": float(self.gross_exposure),
            "net_exposure": float(self.net_exposure),
            "position_count": self.position_count,
        }


def value_portfolio(
    cash: Decimal,
    positions: dict[str, PositionState],
    prices: dict[str, Decimal],
) -> PortfolioValuation:
    """Mark a portfolio to market.

    A position with no available price is carried at its cost basis rather than
    dropped; silently valuing it at zero would put a fake crater in the equity
    curve every time a quote went missing.
    """
    positions_value = ZERO
    unrealized = ZERO
    gross = ZERO
    net = ZERO
    count = 0

    for symbol, state in positions.items():
        if state.is_flat:
            continue
        count += 1
        price = prices.get(symbol)
        if price is None or price <= ZERO:
            price = state.avg_entry_price
        value = state.market_value(price)
        positions_value += value
        unrealized += state.unrealized_pnl(price)
        gross += abs(value)
        net += value

    equity = q_money(cash + positions_value)
    return PortfolioValuation(
        cash=q_money(cash),
        positions_value=q_money(positions_value),
        equity=equity,
        unrealized_pnl=q_money(unrealized),
        gross_exposure=q_money(gross),
        net_exposure=q_money(net),
        position_count=count,
    )


def max_affordable_quantity(
    cash: Decimal,
    price: Decimal,
    per_share: Decimal,
    minimum_commission: Decimal,
    maximum_pct: Decimal,
    fractional: bool,
) -> Decimal:
    """Largest buy that still leaves cash non-negative once fees are charged.

    Solved by shrinking a first estimate rather than algebraically, because the
    commission function has a floor and a cap and is therefore not linear.
    """
    cash = Decimal(cash)
    price = Decimal(price)
    if cash <= ZERO or price <= ZERO:
        return ZERO

    estimate = cash / price
    for _ in range(8):
        if estimate <= ZERO:
            return ZERO
        # Round *down* before checking affordability, never after: rounding a
        # validated quantity up is how a simulated account goes overdrawn by a
        # fraction of a cent on every single buy.
        quantity = (
            estimate.quantize(QTY_DP, rounding=ROUND_DOWN)
            if fractional
            else Decimal(int(estimate))
        )
        if quantity <= ZERO:
            return ZERO
        fee = commission_for(quantity, price, per_share, minimum_commission, maximum_pct)
        if quantity * price + fee <= cash:
            return quantity
        # Shave off the overshoot and retry.
        estimate = (cash - fee) / price
    return ZERO


def infer_position_state(
    quantity: Optional[Decimal],
    avg_entry_price: Optional[Decimal],
    realized_pnl: Optional[Decimal] = None,
) -> PositionState:
    return PositionState(
        quantity=q_qty(Decimal(quantity or 0)),
        avg_entry_price=q_money(Decimal(avg_entry_price or 0)),
        realized_pnl=q_money(Decimal(realized_pnl or 0)),
    )
