"""Non-LLM baselines.

Without a reference, a P&L number means nothing. These two provide it:

* **Buy & Hold SPY** — the thing every active strategy has to beat, and a
  permanent sanity check on the whole system. Its return must track the index;
  if it drifts, the bug is in the engine or the metrics, not in the market.
* **Quant SMA** — a fixed moving-average rule with no model behind it. It
  separates "the LLM added judgement" from "any systematic rule would have
  worked in this regime".

Both record a `decisions` row like every other agent, with zero tokens and zero
shadow cost. That is what makes cost-per-dollar-of-P&L comparable across the
whole roster.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from app.agents.base import AgentContext, new_decision
from app.core.logging import get_logger
from app.db.models import Decision, DecisionKind, DecisionStatus, OrderSide, OrderType
from app.engine.risk import OrderIntent
from app.market import repository
from app.market.screener import ScreenCriteria, screen
from app.market.universe import BENCHMARK_SYMBOL

log = get_logger(__name__)


class BuyAndHoldRuntime:
    """Buy the benchmark once with everything, then never trade again."""

    name = "buy_and_hold"

    def __init__(self, symbol: str = BENCHMARK_SYMBOL) -> None:
        self.symbol = symbol

    async def run(self, ctx: AgentContext) -> Optional[Decision]:
        # Only acts during a trading cycle; the other phases are no-ops for it.
        if ctx.kind is not DecisionKind.TRADE:
            return None

        positions = await ctx.engine.load_positions(ctx.portfolio.id)
        if self.symbol in positions:
            return None  # Already invested. That is the whole strategy.

        price_row = await repository.reference_price(
            ctx.session, self.symbol, as_of=ctx.as_of
        )
        if price_row is None:
            log.warning("baseline.no_price", agent=ctx.agent.slug, symbol=self.symbol)
            return None

        decision = new_decision(ctx, model=None)
        decision.status = DecisionStatus.OK
        decision.market_view = f"Passive exposure to {self.symbol}."
        decision.reasoning = (
            f"Deploying the full account into {self.symbol} and holding it. This is the "
            "benchmark every other agent is measured against."
        )
        decision.output = {"action": "INITIAL_ALLOCATION", "symbol": self.symbol}
        ctx.session.add(decision)
        await ctx.session.flush()

        # Explicit weight of 1.0: this agent's risk overrides allow a 100%
        # position, which is the only way its return can track the index.
        intent = OrderIntent(
            symbol=self.symbol,
            side=OrderSide.BUY,
            weight=Decimal("1"),
            order_type=OrderType.MARKET,
            rationale="Initial full allocation to the benchmark.",
        )
        await ctx.submit_order(intent, decision.id)
        return decision


class QuantSmaRuntime:
    """Deterministic moving-average rule. No model, no judgement, ~zero cost.

    Entry: price above the 50-day and the 50-day above the 200-day (an
    established uptrend), ranked by momentum score.
    Exit: price closes back below the 50-day.
    Equal weight across at most `max_positions`.
    """

    name = "quant_sma"

    def __init__(self, max_positions: int = 5) -> None:
        self.max_positions = max_positions

    async def run(self, ctx: AgentContext) -> Optional[Decision]:
        if ctx.kind is not DecisionKind.TRADE:
            return None

        config = ctx.agent.config or {}
        max_positions = int(config.get("max_positions", self.max_positions))

        positions = await ctx.engine.load_positions(ctx.portfolio.id)
        held = sorted(positions.keys())

        exits = await self._exits(ctx, held)
        entries = await self._entries(ctx, held, max_positions - (len(held) - len(exits)))

        if not exits and not entries:
            return None

        decision = new_decision(ctx, model=None)
        decision.status = DecisionStatus.OK
        decision.market_view = "Rule-based: 50/200 SMA trend filter."
        decision.reasoning = _explain(exits, entries)
        decision.output = {
            "exits": [e["symbol"] for e in exits],
            "entries": [e["symbol"] for e in entries],
            "rule": "enter: price>SMA50 and SMA50>SMA200; exit: price<SMA50",
        }
        ctx.session.add(decision)
        await ctx.session.flush()

        # Exits first, so the cash they free is available for the entries.
        for item in exits:
            await ctx.submit_order(
                OrderIntent(
                    symbol=item["symbol"],
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    rationale=item["reason"],
                ),
                decision.id,
            )

        if entries:
            weight = Decimal(1) / Decimal(max(max_positions, 1))
            for item in entries:
                await ctx.submit_order(
                    OrderIntent(
                        symbol=item["symbol"],
                        side=OrderSide.BUY,
                        weight=weight,
                        order_type=OrderType.MARKET,
                        rationale=item["reason"],
                    ),
                    decision.id,
                )
        return decision

    async def _exits(self, ctx: AgentContext, held: list[str]) -> list[dict[str, Any]]:
        if not held:
            return []
        criteria = ScreenCriteria(
            preset="momentum", limit=len(held), symbols=held, min_price=None,
            min_dollar_volume=None,
        )
        rows = {c.symbol: c.signals for c in await screen(ctx.session, criteria)}
        exits = []
        for symbol in held:
            signals = rows.get(symbol)
            if signals is None:
                continue
            if signals.get("above_sma_50") is False:
                exits.append(
                    {"symbol": symbol, "reason": "price closed back below the 50-day average"}
                )
        return exits

    async def _entries(
        self, ctx: AgentContext, held: list[str], slots: int
    ) -> list[dict[str, Any]]:
        if slots <= 0:
            return []
        criteria = ScreenCriteria(
            preset="momentum",
            limit=slots,
            above_sma_200=True,
            exclude_symbols=held,
        )
        candidates = await screen(ctx.session, criteria)
        entries = []
        for candidate in candidates:
            signals = candidate.signals
            if not signals.get("above_sma_50"):
                continue
            entries.append(
                {
                    "symbol": candidate.symbol,
                    "reason": "uptrend: price above the 50-day, 50-day above the 200-day",
                }
            )
        return entries[:slots]


def _explain(exits: list[dict[str, Any]], entries: list[dict[str, Any]]) -> str:
    parts = []
    if exits:
        parts.append("Exiting " + ", ".join(e["symbol"] for e in exits) + " (below 50-day).")
    if entries:
        parts.append("Entering " + ", ".join(e["symbol"] for e in entries) + " (trend intact).")
    return " ".join(parts) or "No rule triggered."
