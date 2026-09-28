"""The tools an agent can call.

Agents are given tools rather than one enormous prompt: with 500 names in the
universe, the only workable shape is "narrow the field, then look closely at a
handful". Every tool returns pre-computed numbers — the arithmetic has already
happened in Python — so the model spends its budget on judgement, not on
recalculating an RSI badly.

Tool results are deliberately compact. Each extra field is prompt tokens on
every call, and prompt tokens are latency on a local model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import OrderSide, OrderType
from app.engine.risk import OrderIntent
from app.market import repository
from app.market.indicators import compute_signals, frame_from_bars
from app.market.screener import PRESETS, ScreenCriteria, screen
from app.market.universe import BENCHMARK_SYMBOL

log = get_logger(__name__)

MAX_BARS_RETURNED = 10
MAX_NEWS_RETURNED = 8


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[dict[str, Any]]]
    #: `place_order` is off by default — orders travel through the final
    #: structured decision so they can be sized and validated deterministically
    #: before touching the ledger. See `settings.expose_place_order_tool`.
    exposed: bool = True

    def to_openai_spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    pass


# --------------------------------------------------------------- implementations


async def screen_stocks(
    ctx,
    preset: str = "momentum",
    limit: int = 15,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_rsi: Optional[float] = None,
    max_rsi: Optional[float] = None,
    above_sma_200: Optional[bool] = None,
    sectors: Optional[list[str]] = None,
    exclude_held: bool = False,
) -> dict[str, Any]:
    criteria = ScreenCriteria(
        preset=preset,
        limit=limit,
        min_rsi=min_rsi,
        max_rsi=max_rsi,
        above_sma_200=above_sma_200,
        sectors=sectors or [],
    )
    if min_price is not None:
        criteria.min_price = min_price
    if max_price is not None:
        criteria.max_price = max_price
    if exclude_held:
        criteria.exclude_symbols = list(await ctx.held_symbols())

    candidates = await screen(ctx.session, criteria)
    return {
        "preset": criteria.preset,
        "count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
    }


async def get_bars(ctx, symbol: str, days: int = 60) -> dict[str, Any]:
    symbol = symbol.upper()
    lookback = max(30, min(int(days or 60), 400))
    # Pull extra history so the long indicators are populated, but only hand
    # back the tail: a local model does not need 250 rows of OHLCV.
    bars = await repository.get_bars(
        ctx.session, symbol, limit=max(lookback, 260), as_of=ctx.as_of
    )
    if not bars:
        return {"symbol": symbol, "error": "no price history available"}

    frame = frame_from_bars(bars)
    signals = compute_signals(frame)
    recent = [
        {
            "date": b.ts.date().isoformat(),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume or 0),
        }
        for b in bars[-MAX_BARS_RETURNED:]
    ]
    return {
        "symbol": symbol,
        "bars_available": len(bars),
        "recent_bars": recent,
        "signals": signals,
    }


async def get_quote(ctx, symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    quote = await repository.latest_quote(ctx.session, symbol, as_of=ctx.as_of)
    if quote is None:
        reference = await repository.reference_price(ctx.session, symbol, as_of=ctx.as_of)
        if reference is None:
            return {"symbol": symbol, "error": "no price available"}
        price, ts, source = reference
        return {
            "symbol": symbol,
            "price": float(price),
            "as_of": ts.isoformat(),
            "source": source,
            "stale": True,
        }

    change_pct = None
    if quote.prev_close and quote.prev_close > 0:
        change_pct = round(float(quote.price / quote.prev_close - 1) * 100, 3)
    return {
        "symbol": symbol,
        "price": float(quote.price),
        "prev_close": float(quote.prev_close) if quote.prev_close else None,
        "change_pct": change_pct,
        "day_high": float(quote.day_high) if quote.day_high else None,
        "day_low": float(quote.day_low) if quote.day_low else None,
        "as_of": quote.ts.isoformat(),
        "source": quote.source,
    }


async def get_news(ctx, symbol: str, limit: int = 5) -> dict[str, Any]:
    symbol = symbol.upper()
    limit = max(1, min(int(limit or 5), MAX_NEWS_RETURNED))
    items = await repository.get_news(ctx.session, symbol, limit=limit, as_of=ctx.as_of)
    return {
        "symbol": symbol,
        "count": len(items),
        "news": [
            {
                "headline": item.headline,
                "summary": item.summary[:280],
                "source": item.source,
                "published_at": item.published_at.isoformat(),
                "age_hours": round(
                    (ctx.now - item.published_at).total_seconds() / 3600, 1
                ),
            }
            for item in items
        ],
    }


async def get_portfolio(ctx) -> dict[str, Any]:
    valuation = await ctx.engine.valuation(ctx.portfolio)
    positions = await ctx.engine.load_positions(ctx.portfolio.id)
    prices = await repository.reference_prices(
        ctx.session, list(positions.keys()), as_of=ctx.as_of
    )

    rows = []
    for symbol, position in positions.items():
        price = prices.get(symbol, Decimal(position.avg_entry_price))
        quantity = Decimal(position.quantity)
        value = quantity * price
        cost = quantity * Decimal(position.avg_entry_price)
        rows.append(
            {
                "symbol": symbol,
                "quantity": float(quantity),
                "avg_entry_price": float(position.avg_entry_price),
                "price": float(price),
                "market_value": round(float(value), 2),
                "unrealized_pnl": round(float(value - cost), 2),
                "unrealized_pnl_pct": round(float(value / cost - 1) * 100, 2) if cost else 0.0,
                "weight_pct": round(float(value / valuation.equity) * 100, 2)
                if valuation.equity
                else 0.0,
            }
        )
    rows.sort(key=lambda r: r["market_value"], reverse=True)

    return {
        "equity": round(float(valuation.equity), 2),
        "cash": round(float(valuation.cash), 2),
        "positions_value": round(float(valuation.positions_value), 2),
        "unrealized_pnl": round(float(valuation.unrealized_pnl), 2),
        "realized_pnl": round(float(ctx.portfolio.realized_pnl), 2),
        "initial_capital": round(float(ctx.portfolio.initial_capital), 2),
        "total_return_pct": round(
            float(valuation.equity / Decimal(ctx.portfolio.initial_capital) - 1) * 100, 3
        ),
        "position_count": valuation.position_count,
        "positions": rows,
        "limits": {
            "max_position_pct": settings.max_position_pct,
            "max_orders_per_cycle": settings.max_orders_per_cycle,
            "shorting_allowed": settings.allow_short,
        },
    }


async def get_market_context(ctx) -> dict[str, Any]:
    """Benchmark state, so an agent can tell its own moves from the market's."""
    quote = await repository.latest_quote(ctx.session, BENCHMARK_SYMBOL, as_of=ctx.as_of)
    bars = await repository.get_bars(ctx.session, BENCHMARK_SYMBOL, limit=260, as_of=ctx.as_of)
    signals = compute_signals(frame_from_bars(bars)) if bars else {}
    return {
        "benchmark": BENCHMARK_SYMBOL,
        "price": float(quote.price) if quote else signals.get("price"),
        "change_pct": signals.get("change_pct"),
        "return_21d_pct": signals.get("return_21d_pct"),
        "above_sma_50": signals.get("above_sma_50"),
        "above_sma_200": signals.get("above_sma_200"),
        "volatility_20d": signals.get("volatility_20d"),
        "phase": ctx.phase.value,
        "as_of": ctx.now.isoformat(),
    }


async def place_order(
    ctx,
    symbol: str,
    action: str,
    conviction: float = 0.5,
    rationale: str = "",
) -> dict[str, Any]:
    """Direct order placement. Disabled by default; see `Tool.exposed`."""
    side = OrderSide.BUY if str(action).upper() == "BUY" else OrderSide.SELL
    intent = OrderIntent(
        symbol=symbol.upper(),
        side=side,
        conviction=float(conviction),
        order_type=OrderType.MARKET,
        rationale=rationale,
    )
    result = await ctx.submit_order(intent)
    return result.summary()


# ------------------------------------------------------------------ catalog


def _tool_catalog() -> dict[str, Tool]:
    preset_names = ", ".join(sorted(PRESETS))
    tools = [
        Tool(
            name="screen_stocks",
            description=(
                "Filter the S&P 500 down to a ranked shortlist with signals already "
                f"calculated. Presets: {preset_names}. Start here — you cannot see the "
                "whole universe at once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": sorted(PRESETS),
                        "description": "Which ranking to apply.",
                    },
                    "limit": {"type": "integer", "description": "How many names (1-50)."},
                    "min_price": {"type": "number"},
                    "max_price": {"type": "number"},
                    "min_rsi": {"type": "number"},
                    "max_rsi": {"type": "number"},
                    "above_sma_200": {"type": "boolean"},
                    "sectors": {"type": "array", "items": {"type": "string"}},
                    "exclude_held": {
                        "type": "boolean",
                        "description": "Skip names you already hold.",
                    },
                },
                "required": ["preset"],
            },
            handler=screen_stocks,
        ),
        Tool(
            name="get_bars",
            description="Recent price history plus the full indicator set for one symbol.",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "days": {"type": "integer", "description": "Lookback in trading days."},
                },
                "required": ["symbol"],
            },
            handler=get_bars,
        ),
        Tool(
            name="get_quote",
            description="Latest price for one symbol.",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            handler=get_quote,
        ),
        Tool(
            name="get_news",
            description="Recent headlines for one symbol, newest first.",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["symbol"],
            },
            handler=get_news,
        ),
        Tool(
            name="get_portfolio",
            description="Your own cash, positions, weights and unrealised P&L.",
            parameters={"type": "object", "properties": {}},
            handler=get_portfolio,
        ),
        Tool(
            name="get_market_context",
            description="How the S&P 500 benchmark itself is behaving right now.",
            parameters={"type": "object", "properties": {}},
            handler=get_market_context,
        ),
        Tool(
            name="place_order",
            description="Submit an order immediately instead of returning it in your decision.",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "conviction": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["symbol", "action"],
            },
            handler=place_order,
            exposed=settings.expose_place_order_tool,
        ),
    ]
    return {tool.name: tool for tool in tools}


TOOLS: dict[str, Tool] = _tool_catalog()


def exposed_tools(names: Optional[list[str]] = None) -> list[Tool]:
    """Tools offered to the model, optionally narrowed to a strategy's subset."""
    available = [t for t in TOOLS.values() if t.exposed]
    if names is None:
        return available
    wanted = set(names)
    return [t for t in available if t.name in wanted]


def openai_tool_specs(names: Optional[list[str]] = None) -> list[dict[str, Any]]:
    return [tool.to_openai_spec() for tool in exposed_tools(names)]


async def dispatch(ctx, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a tool call, converting any failure into a result the model can read."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}", "available": sorted(TOOLS)}
    if not isinstance(arguments, dict):
        return {"error": "arguments must be an object"}
    try:
        return await tool.handler(ctx, **arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, logged here
        log.warning("tool.failed", tool=name, error=str(exc))
        return {"error": f"{name} failed: {exc}"}
