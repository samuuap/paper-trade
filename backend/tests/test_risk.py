"""Deterministic tests for the pre-trade risk layer.

This is the module that makes "the model proposes, Python disposes" true, so
the tests are written as claims about what an agent *cannot* do: it cannot
exceed its position cap, cannot exceed gross exposure, cannot spend cash it
does not have, cannot short when shorting is off, and cannot trade a symbol
outside the set it was given.

Numbers are hand-computed. Where a value depends on the commission model the
assertion is written as the invariant it protects rather than a magic constant,
because a constant would silently encode a bug in the fee schedule as expected
behaviour.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import OrderSide, OrderType
from app.engine.ledger import PositionState, apply_slippage, commission_for
from app.engine.risk import OrderIntent, RiskContext, RiskLimits, evaluate, target_quantity

D = Decimal


def limits(**overrides) -> RiskLimits:
    base = RiskLimits(
        max_position_pct=0.20,
        max_gross_exposure_pct=1.0,
        min_order_notional=100.0,
        max_orders_per_cycle=5,
        cash_buffer_pct=0.02,
        allow_short=False,
        allow_fractional_shares=True,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def context(
    *,
    equity=D("100000"),
    cash=D("100000"),
    positions=None,
    prices=None,
    tradable=None,
    lim=None,
) -> RiskContext:
    return RiskContext(
        equity=equity,
        cash=cash,
        positions=positions or {},
        prices=prices or {"AAPL": D("100"), "MSFT": D("200"), "SPY": D("400")},
        tradable_symbols=tradable,
        limits=lim or limits(),
    )


def buy(symbol="AAPL", **kwargs) -> OrderIntent:
    return OrderIntent(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, **kwargs)


def sell(symbol="AAPL", **kwargs) -> OrderIntent:
    return OrderIntent(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET, **kwargs)


# ------------------------------------------------------------------ sizing


def test_explicit_quantity_is_used_verbatim():
    assert target_quantity(buy(quantity=D("7")), context(), D("100")) == D("7")


def test_notional_is_converted_at_the_reference_price():
    assert target_quantity(buy(notional=D("2500")), context(), D("100")) == D("25")


def test_weight_is_a_fraction_of_equity():
    # 10% of $100,000 at $100 = 100 shares.
    assert target_quantity(buy(weight=D("0.1")), context(), D("100")) == D("100")


def test_conviction_scales_the_position_cap():
    # Full conviction uses the whole 20% allowance: $20,000 at $100 = 200 shares.
    assert target_quantity(buy(conviction=1.0), context(), D("100")) == D("200")
    # Half conviction uses half of it.
    assert target_quantity(buy(conviction=0.5), context(), D("100")) == D("100")


def test_missing_conviction_defaults_to_half_the_allowance():
    assert target_quantity(buy(), context(), D("100")) == D("100")


def test_sell_without_a_size_means_close_the_position():
    positions = {"AAPL": PositionState(quantity=D("13.5"), avg_entry_price=D("90"))}
    assert target_quantity(sell(), context(positions=positions), D("100")) == D("13.5")


def test_a_close_out_sells_the_exact_holding_and_leaves_no_dust():
    """Regression: rounding a close down leaves a sliver behind, for ever.

    A partial fill can leave a position with more precision than the trading
    increment. Rounding that down on the way out means the exit never quite
    exits, and the residue then counts as an open position in the position
    count, the concentration index and the exposure figures.
    """
    held = D("13.500055")
    positions = {"AAPL": PositionState(quantity=held, avg_entry_price=D("90"))}
    ctx = context(positions=positions)

    assert target_quantity(sell(), ctx, D("100")) == held
    verdict = evaluate(sell(), ctx)
    assert verdict.approved
    assert verdict.quantity == held
    # And what is left behind afterwards is nothing at all.
    from app.engine.ledger import apply_fill

    remaining = apply_fill(positions["AAPL"], -verdict.quantity, D("100")).position
    assert remaining.is_flat
    assert remaining.quantity == D("0")


def test_whole_shares_when_fractional_is_disabled():
    ctx = context(lim=limits(allow_fractional_shares=False))
    # 0.1 * 100000 / 300 = 33.33 -> 33
    assert target_quantity(buy(weight=D("0.1")), ctx, D("300")) == D("33")


# -------------------------------------------------------------------- caps


def test_buy_is_clamped_by_the_position_cap():
    verdict = evaluate(buy(weight=D("0.9")), context())
    assert verdict.approved
    # 20% of $100,000 at $100 = 200 shares, not the 900 requested.
    assert verdict.quantity == D("200")
    assert verdict.notes["clamp_reason"] == "position cap"


def test_buy_is_refused_when_already_at_the_position_cap():
    positions = {"AAPL": PositionState(quantity=D("200"), avg_entry_price=D("100"))}
    verdict = evaluate(buy(weight=D("0.1")), context(positions=positions))
    assert verdict.rejected
    assert "position cap" in verdict.reason


def test_existing_position_reduces_the_headroom():
    positions = {"AAPL": PositionState(quantity=D("150"), avg_entry_price=D("100"))}
    verdict = evaluate(buy(weight=D("0.2")), context(positions=positions))
    # Cap is $20,000, $15,000 is already held, so only $5,000 (50 shares) fits.
    assert verdict.approved
    assert verdict.quantity == D("50")


def test_gross_exposure_cap_clamps_the_last_buy():
    positions = {"MSFT": PositionState(quantity=D("475"), avg_entry_price=D("200"))}
    ctx = context(cash=D("20000"), positions=positions, lim=limits(max_gross_exposure_pct=1.0))
    verdict = evaluate(buy(weight=D("0.2")), ctx)
    # $95,000 of gross against a $100,000 cap leaves room for $5,000 = 50 shares.
    assert verdict.approved
    assert verdict.quantity == D("50")
    assert verdict.notes["clamp_reason"] == "gross exposure cap"


def test_cash_buffer_is_reserved():
    ctx = context(cash=D("3000"), lim=limits(cash_buffer_pct=0.02))
    verdict = evaluate(buy(weight=D("0.2")), ctx)
    # $3,000 cash less a 2% ($2,000) buffer leaves $1,000 spendable.
    assert verdict.approved
    assert verdict.notes["spendable_cash"] == pytest.approx(1000.0)
    assert verdict.quantity * D("100") <= D("1000")


def test_no_spendable_cash_after_the_buffer_is_refused():
    verdict = evaluate(buy(weight=D("0.1")), context(cash=D("1500")))
    assert verdict.rejected
    assert "buffer" in verdict.reason


# --------------------------------------------------------------- the fills


def test_sizing_prices_in_slippage_so_a_full_allocation_can_actually_fill():
    """Regression: the Buy & Hold baseline must be able to spend everything.

    Sizing at the reference price while filling a few basis points above it
    left every 100%-of-cash order a fraction short at the ledger, and the only
    agent that always does that is the benchmark — the one whose correctness
    validates the rest of the system.
    """
    ctx = context(
        lim=limits(max_position_pct=1.0, cash_buffer_pct=0.0, min_order_notional=100.0)
    )
    verdict = evaluate(buy("SPY", weight=D("1")), ctx)
    assert verdict.approved

    fill_price = apply_slippage(D("400"), True, D(str(settings.slippage_bps)))
    fee = commission_for(
        verdict.quantity,
        fill_price,
        D(str(settings.commission_per_share)),
        D(str(settings.commission_minimum)),
        D(str(settings.commission_maximum_pct)),
    )
    assert verdict.quantity * fill_price + fee <= ctx.cash


def test_the_expected_fill_price_is_recorded_for_audit():
    verdict = evaluate(buy(weight=D("0.1")), context())
    assert verdict.notes["expected_fill_price"] > verdict.notes["reference_price"]


# ------------------------------------------------------------------- sells


def test_sell_without_a_position_is_refused_when_shorting_is_off():
    verdict = evaluate(sell(quantity=D("10")), context())
    assert verdict.rejected
    assert "shorting disabled" in verdict.reason


def test_oversized_sell_is_clamped_to_the_position():
    positions = {"AAPL": PositionState(quantity=D("30"), avg_entry_price=D("90"))}
    verdict = evaluate(sell(quantity=D("100")), context(positions=positions))
    assert verdict.approved
    assert verdict.quantity == D("30")
    assert verdict.notes["clamp_reason"] == "shorting disabled"


def test_a_full_close_is_allowed_below_the_minimum_notional():
    """Blocking an exit on a size rule would be a genuinely dangerous rule."""
    positions = {"AAPL": PositionState(quantity=D("0.5"), avg_entry_price=D("90"))}
    verdict = evaluate(sell(), context(positions=positions))
    assert verdict.approved
    assert verdict.quantity == D("0.5")


def test_a_partial_sell_below_the_minimum_notional_is_refused():
    positions = {"AAPL": PositionState(quantity=D("30"), avg_entry_price=D("90"))}
    verdict = evaluate(sell(quantity=D("0.5")), context(positions=positions))
    assert verdict.rejected
    assert "below minimum" in verdict.reason


# ------------------------------------------------------------- validation


def test_a_symbol_outside_the_tradable_set_is_refused():
    verdict = evaluate(buy("TSLA", weight=D("0.1")), context(tradable={"AAPL", "MSFT"}))
    assert verdict.rejected
    assert "not in the tradable universe" in verdict.reason


def test_a_symbol_without_a_price_is_refused():
    verdict = evaluate(buy("NFLX", weight=D("0.1")), context())
    assert verdict.rejected
    assert "no reference price" in verdict.reason


def test_a_limit_order_without_a_limit_price_is_refused():
    intent = OrderIntent(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT, weight=D("0.1")
    )
    verdict = evaluate(intent, context())
    assert verdict.rejected
    assert "limit price" in verdict.reason


def test_a_buy_below_the_minimum_notional_is_refused():
    verdict = evaluate(buy(notional=D("50")), context())
    assert verdict.rejected
    assert "below minimum" in verdict.reason


def test_zero_sizing_is_refused_rather_than_silently_passed_through():
    verdict = evaluate(buy(quantity=D("0.00001")), context())
    assert verdict.rejected


# ------------------------------------------------------------ agent limits


def test_agent_config_overrides_the_global_limits():
    resolved = RiskLimits.for_agent({"risk": {"max_position_pct": 1.0, "cash_buffer_pct": 0.0}})
    assert resolved.max_position_pct == 1.0
    assert resolved.cash_buffer_pct == 0.0
    # Untouched keys keep the global default.
    assert resolved.min_order_notional == settings.min_order_notional


def test_agent_config_ignores_unknown_and_null_overrides():
    resolved = RiskLimits.for_agent({"risk": {"nonsense": 5, "max_position_pct": None}})
    assert resolved.max_position_pct == settings.max_position_pct
    assert not hasattr(resolved, "nonsense")
