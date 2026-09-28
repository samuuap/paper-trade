"""Read/write helpers for market data.

Everything downstream — agents, tools, the execution engine, metrics — reads
prices through this module, never from a provider. Two consequences that matter:
the decision loop is decoupled from network latency, and every read accepts an
`as_of` so a past session can be replayed exactly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketBar, MarketQuote, NewsItem
from app.db.upsert import upsert
from app.market.provider import BarData, NewsData, QuoteData

UTC = dt.timezone.utc


# ---------------------------------------------------------------- write path


async def upsert_bars(session: AsyncSession, bars: Sequence[BarData]) -> int:
    if not bars:
        return 0
    rows = [
        {
            "symbol": b.symbol,
            "timeframe": b.timeframe,
            "ts": b.ts,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "source": b.source,
        }
        for b in bars
    ]
    return await upsert(
        session,
        MarketBar.__table__,
        rows,
        index_elements=[MarketBar.symbol, MarketBar.timeframe, MarketBar.ts],
        update_columns=["open", "high", "low", "close", "volume", "source"],
    )


async def record_quotes(session: AsyncSession, quotes: Sequence[QuoteData]) -> int:
    if not quotes:
        return 0
    rows = [
        {
            "symbol": q.symbol,
            "ts": q.ts,
            "price": q.price,
            "prev_close": q.prev_close,
            "day_open": q.day_open,
            "day_high": q.day_high,
            "day_low": q.day_low,
            "volume": None,
            "source": q.source,
        }
        for q in quotes
    ]
    # Two ticks can share a timestamp; last write wins rather than blowing up.
    return await upsert(
        session,
        MarketQuote.__table__,
        rows,
        index_elements=[MarketQuote.symbol, MarketQuote.ts],
        update_columns=["price", "source"],
    )


async def upsert_news(session: AsyncSession, items: Sequence[NewsData]) -> int:
    if not items:
        return 0
    rows = [
        {
            "symbol": n.symbol,
            "external_id": n.external_id,
            "headline": n.headline,
            "summary": n.summary,
            "source": n.source,
            "url": n.url,
            "published_at": n.published_at,
        }
        for n in items
    ]
    return await upsert(
        session,
        NewsItem.__table__,
        rows,
        index_elements=[NewsItem.symbol, NewsItem.external_id],
    )


# ----------------------------------------------------------------- read path


async def latest_quote(
    session: AsyncSession, symbol: str, as_of: Optional[dt.datetime] = None
) -> Optional[MarketQuote]:
    stmt = select(MarketQuote).where(MarketQuote.symbol == symbol)
    if as_of is not None:
        stmt = stmt.where(MarketQuote.ts <= as_of)
    stmt = stmt.order_by(desc(MarketQuote.ts)).limit(1)
    return (await session.execute(stmt)).scalars().first()


def _latest_per_symbol(model, symbols: Sequence[str], as_of: Optional[dt.datetime], *extra_where):
    """Newest row per symbol, as a window function.

    `DISTINCT ON` would be the natural Postgres answer, but a `row_number()`
    window runs on Postgres and SQLite alike and lets the same query serve
    production and the offline test/replay database.
    """
    ranked = select(
        model,
        func.row_number()
        .over(partition_by=model.symbol, order_by=desc(model.ts))
        .label("_rank"),
    ).where(model.symbol.in_(list(symbols)), *extra_where)
    if as_of is not None:
        ranked = ranked.where(model.ts <= as_of)
    subquery = ranked.subquery()
    return select(subquery).where(subquery.c._rank == 1), subquery


async def latest_quotes(
    session: AsyncSession, symbols: Iterable[str], as_of: Optional[dt.datetime] = None
) -> dict[str, MarketQuote]:
    """One row per symbol, newest first."""
    symbol_list = list(symbols)
    if not symbol_list:
        return {}
    stmt, _ = _latest_per_symbol(MarketQuote, symbol_list, as_of)
    rows = (await session.execute(stmt)).mappings().all()
    return {
        row["symbol"]: MarketQuote(
            symbol=row["symbol"],
            ts=row["ts"],
            price=row["price"],
            prev_close=row["prev_close"],
            day_open=row["day_open"],
            day_high=row["day_high"],
            day_low=row["day_low"],
            volume=row["volume"],
            source=row["source"],
        )
        for row in rows
    }


async def get_bars(
    session: AsyncSession,
    symbol: str,
    timeframe: str = "1D",
    limit: int = 250,
    as_of: Optional[dt.datetime] = None,
) -> list[MarketBar]:
    """Most recent `limit` bars, returned oldest-first for indicator maths."""
    stmt = select(MarketBar).where(
        MarketBar.symbol == symbol, MarketBar.timeframe == timeframe
    )
    if as_of is not None:
        stmt = stmt.where(MarketBar.ts <= as_of)
    stmt = stmt.order_by(desc(MarketBar.ts)).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return list(reversed(rows))


async def get_bars_bulk(
    session: AsyncSession,
    symbols: Sequence[str],
    timeframe: str = "1D",
    lookback_days: int = 400,
    as_of: Optional[dt.datetime] = None,
) -> dict[str, list[MarketBar]]:
    if not symbols:
        return {}
    cutoff = (as_of or dt.datetime.now(UTC)) - dt.timedelta(days=lookback_days)
    stmt = select(MarketBar).where(
        MarketBar.symbol.in_(list(symbols)),
        MarketBar.timeframe == timeframe,
        MarketBar.ts >= cutoff,
    )
    if as_of is not None:
        stmt = stmt.where(MarketBar.ts <= as_of)
    stmt = stmt.order_by(MarketBar.symbol, MarketBar.ts)
    grouped: dict[str, list[MarketBar]] = {}
    for bar in (await session.execute(stmt)).scalars().all():
        grouped.setdefault(bar.symbol, []).append(bar)
    return grouped


async def reference_price(
    session: AsyncSession, symbol: str, as_of: Optional[dt.datetime] = None
) -> Optional[tuple[Decimal, dt.datetime, str]]:
    """The price a fill should be simulated against.

    Prefers a live quote; falls back to the last daily close so that orders
    placed outside the live-feed window (or for a name that rotated off the
    websocket) still have a defensible reference rather than silently failing.
    Returns `(price, timestamp, source)`.
    """
    quote = await latest_quote(session, symbol, as_of=as_of)
    if quote is not None and quote.price and quote.price > 0:
        return Decimal(quote.price), quote.ts, "quote"

    bars = await get_bars(session, symbol, limit=1, as_of=as_of)
    if bars:
        bar = bars[-1]
        return Decimal(bar.close), bar.ts, "bar_close"
    return None


async def reference_prices(
    session: AsyncSession, symbols: Sequence[str], as_of: Optional[dt.datetime] = None
) -> dict[str, Decimal]:
    """Bulk variant used for marking portfolios to market."""
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}
    prices: dict[str, Decimal] = {}
    quotes = await latest_quotes(session, symbols, as_of=as_of)
    for symbol, quote in quotes.items():
        if quote.price and quote.price > 0:
            prices[symbol] = Decimal(quote.price)

    missing = [s for s in symbols if s not in prices]
    if missing:
        bar_stmt, _ = _latest_per_symbol(
            MarketBar, missing, as_of, MarketBar.timeframe == "1D"
        )
        for row in (await session.execute(bar_stmt)).mappings().all():
            prices[row["symbol"]] = Decimal(row["close"])
    return prices


async def get_news(
    session: AsyncSession,
    symbol: str,
    limit: int = 10,
    as_of: Optional[dt.datetime] = None,
) -> list[NewsItem]:
    stmt = select(NewsItem).where(NewsItem.symbol == symbol)
    if as_of is not None:
        stmt = stmt.where(NewsItem.published_at <= as_of)
    stmt = stmt.order_by(desc(NewsItem.published_at)).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def data_lag_seconds(session: AsyncSession) -> Optional[float]:
    """Age of the freshest quote in the store — the core data-health metric."""
    newest = (await session.execute(select(func.max(MarketQuote.ts)))).scalar()
    if newest is None:
        return None
    # SQLite has no timezone-aware storage, so values come back naive. They are
    # always written as UTC, so reattaching the zone is a restoration rather
    # than an assumption.
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    return (dt.datetime.now(UTC) - newest).total_seconds()


async def bar_coverage(session: AsyncSession) -> dict[str, object]:
    newest = (await session.execute(select(func.max(MarketBar.ts)))).scalar()
    symbols = (
        await session.execute(select(func.count(func.distinct(MarketBar.symbol))))
    ).scalar()
    total = (await session.execute(select(func.count()).select_from(MarketBar))).scalar()
    return {
        "latest_bar_ts": newest.isoformat() if newest else None,
        "symbols_with_bars": int(symbols or 0),
        "total_bars": int(total or 0),
    }
