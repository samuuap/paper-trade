"""Deterministic tests for the accounting primitives.

Every case here is hand-computed. A bug in this module would not throw — it
would quietly skew every metric in the system — so the expected numbers are
written out literally rather than derived from the code under test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.ledger import (
    LedgerError,
    PositionState,
    apply_fill,
    apply_slippage,
    commission_for,
    max_affordable_quantity,
    value_portfolio,
)

D = Decimal


def test_open_long_from_flat():
    result = apply_fill(PositionState(), D("10"), D("100"))
    assert result.position.quantity == D("10")
    assert result.position.avg_entry_price == D("100")
    assert result.realized_pnl == D("0")
    assert result.trade_cash_delta == D("-1000")


def test_scaling_in_reweights_average_entry():
    state = apply_fill(PositionState(), D("10"), D("100")).position
    state = apply_fill(state, D("30"), D("120")).position
    # (10*100 + 30*120) / 40 = 115
    assert state.quantity == D("40")
    assert state.avg_entry_price == D("115")


def test_partial_close_realises_pnl_and_keeps_average():
    state = PositionState(quantity=D("40"), avg_entry_price=D("115"))
    result = apply_fill(state, D("-10"), D("130"))
    # (130 - 115) * 10 = 150 realised; the remaining 30 keep their basis.
    assert result.realized_pnl == D("150")
    assert result.position.quantity == D("30")
    assert result.position.avg_entry_price == D("115")
    assert result.trade_cash_delta == D("1300")


def test_full_close_zeroes_position_and_accumulates_realised():
    state = PositionState(quantity=D("30"), avg_entry_price=D("115"), realized_pnl=D("150"))
    result = apply_fill(state, D("-30"), D("110"))
    # (110 - 115) * 30 = -150 on this trade; running total returns to zero.
    assert result.realized_pnl == D("-150")
    assert result.position.quantity == D("0")
    assert result.position.avg_entry_price == D("0")
    assert result.position.realized_pnl == D("0")


def test_unrealised_pnl_is_marked_against_average_entry():
    state = PositionState(quantity=D("30"), avg_entry_price=D("115"))
    assert state.unrealized_pnl(D("125")) == D("300")
    assert state.unrealized_pnl(D("105")) == D("-300")
    assert PositionState().unrealized_pnl(D("105")) == D("0")


def test_round_trip_realised_plus_fees_matches_cash_movement():
    """The sanity identity: cash out + cash in == realised P&L - fees."""
    buy = apply_fill(PositionState(), D("10"), D("100"))
    sell = apply_fill(buy.position, D("-10"), D("110"))
    buy_fee = commission_for(D("10"), D("100"), D("0.005"), D("1"), D("0.005"))
    sell_fee = commission_for(D("10"), D("110"), D("0.005"), D("1"), D("0.005"))

    cash_movement = (buy.trade_cash_delta - buy_fee) + (sell.trade_cash_delta - sell_fee)
    assert cash_movement == sell.realized_pnl - buy_fee - sell_fee
    assert sell.realized_pnl == D("100")


def test_short_then_cover_realises_inverted_pnl():
    opened = apply_fill(PositionState(), D("-10"), D("100"))
    assert opened.position.quantity == D("-10")
    assert opened.position.avg_entry_price == D("100")
    assert opened.trade_cash_delta == D("1000")

    covered = apply_fill(opened.position, D("10"), D("90"))
    # Short from 100, covered at 90 => +10/share.
    assert covered.realized_pnl == D("100")
    assert covered.position.quantity == D("0")


def test_flip_through_zero_opens_remainder_at_fill_price():
    state = PositionState(quantity=D("10"), avg_entry_price=D("100"))
    result = apply_fill(state, D("-25"), D("110"))
    # Closes the 10 long at +10/share, leaves a 15 short opened at 110.
    assert result.realized_pnl == D("100")
    assert result.position.quantity == D("-15")
    assert result.position.avg_entry_price == D("110")


def test_fractional_quantities_keep_precision():
    result = apply_fill(PositionState(), D("0.5"), D("333.33"))
    assert result.position.quantity == D("0.5")
    assert result.trade_cash_delta == D("-166.665")


@pytest.mark.parametrize("bad_quantity", [D("0")])
def test_zero_quantity_rejected(bad_quantity):
    with pytest.raises(LedgerError):
        apply_fill(PositionState(), bad_quantity, D("100"))


def test_non_positive_price_rejected():
    with pytest.raises(LedgerError):
        apply_fill(PositionState(), D("1"), D("0"))


# ---------------------------------------------------------------- fee model


def test_commission_applies_floor():
    # 10 shares * $0.005 = $0.05, below the $1 minimum.
    assert commission_for(D("10"), D("100"), D("0.005"), D("1"), D("0.005")) == D("1")


def test_commission_applies_percentage_ceiling():
    # 10,000 shares of a $0.50 stock: per-share fee would be $50 on a $5,000
    # notional; the 0.5% ceiling caps it at $25.
    fee = commission_for(D("10000"), D("0.50"), D("0.005"), D("1"), D("0.005"))
    assert fee == D("25")


def test_commission_scales_with_size_between_floor_and_ceiling():
    assert commission_for(D("1000"), D("100"), D("0.005"), D("1"), D("0.005")) == D("5")


def test_slippage_always_works_against_the_trader():
    assert apply_slippage(D("100"), is_buy=True, slippage_bps=D("5")) == D("100.05")
    assert apply_slippage(D("100"), is_buy=False, slippage_bps=D("5")) == D("99.95")


# ------------------------------------------------------------ affordability


def test_max_affordable_quantity_leaves_cash_non_negative():
    cash = D("1000")
    price = D("97.37")
    quantity = max_affordable_quantity(
        cash, price, D("0.005"), D("1"), D("0.005"), fractional=False
    )
    fee = commission_for(quantity, price, D("0.005"), D("1"), D("0.005"))
    assert quantity > 0
    assert quantity * price + fee <= cash
    # One more share must not fit.
    bigger = quantity + 1
    bigger_fee = commission_for(bigger, price, D("0.005"), D("1"), D("0.005"))
    assert bigger * price + bigger_fee > cash


def test_max_affordable_quantity_handles_fractional():
    quantity = max_affordable_quantity(
        D("1000"), D("97.37"), D("0.005"), D("1"), D("0.005"), fractional=True
    )
    fee = commission_for(quantity, D("97.37"), D("0.005"), D("1"), D("0.005"))
    assert quantity * D("97.37") + fee <= D("1000")


def test_max_affordable_quantity_returns_zero_when_broke():
    assert max_affordable_quantity(
        D("0.10"), D("500"), D("0.005"), D("1"), D("0.005"), fractional=False
    ) == D("0")


# -------------------------------------------------------------- valuation


def test_value_portfolio_marks_to_market():
    positions = {
        "AAPL": PositionState(quantity=D("10"), avg_entry_price=D("100")),
        "MSFT": PositionState(quantity=D("5"), avg_entry_price=D("200")),
    }
    prices = {"AAPL": D("110"), "MSFT": D("190")}
    valuation = value_portfolio(D("5000"), positions, prices)

    assert valuation.positions_value == D("2050")  # 1100 + 950
    assert valuation.equity == D("7050")
    assert valuation.unrealized_pnl == D("50")  # +100 -50
    assert valuation.gross_exposure == D("2050")
    assert valuation.net_exposure == D("2050")
    assert valuation.position_count == 2


def test_value_portfolio_falls_back_to_cost_when_price_missing():
    """A missing quote must not punch a fake hole in the equity curve."""
    positions = {"AAPL": PositionState(quantity=D("10"), avg_entry_price=D("100"))}
    valuation = value_portfolio(D("1000"), positions, prices={})
    assert valuation.positions_value == D("1000")
    assert valuation.equity == D("2000")
    assert valuation.unrealized_pnl == D("0")


def test_value_portfolio_nets_longs_against_shorts():
    positions = {
        "AAPL": PositionState(quantity=D("10"), avg_entry_price=D("100")),
        "TSLA": PositionState(quantity=D("-4"), avg_entry_price=D("250")),
    }
    prices = {"AAPL": D("100"), "TSLA": D("250")}
    valuation = value_portfolio(D("0"), positions, prices)
    assert valuation.gross_exposure == D("2000")
    assert valuation.net_exposure == D("0")
    assert valuation.position_count == 2


def test_a_residue_below_the_trading_increment_counts_as_flat():
    """Anything under 0.0001 shares cannot be traded, so it is not a position."""
    from app.engine.ledger import DUST

    assert PositionState(quantity=DUST / 2).is_flat
    assert not PositionState(quantity=DUST).is_flat
    assert PositionState(quantity=-DUST / 2).is_flat


def test_flat_positions_are_ignored_in_valuation():
    positions = {"AAPL": PositionState(quantity=D("0"), avg_entry_price=D("0"))}
    valuation = value_portfolio(D("500"), positions, {"AAPL": D("100")})
    assert valuation.position_count == 0
    assert valuation.equity == D("500")
