"""Market-data ingest jobs.

The two-tier shape the free tier forces on us, and which happens to fit the
phase machine exactly:

* **Daily bars for the whole universe, once, after the close.** ~500 requests at
  55/min is about nine minutes — irrelevant during `AFTER_HOURS`, where nothing
  is waiting on it. Everything the screener and the indicators need comes from
  this pass.
* **Quotes for the active set only, frequently, while the market is open.** The
  active set is "what the six portfolios actually hold, plus the best candidates
  of the day", which is what the 50-symbol websocket cap affords anyway.

Nothing here is read by an agent directly: ingest writes to the database and
agents read from it (see `market/repository.py`). That indirection is what makes
a cycle reproducible and a replay possible.

**Known limitation of the free tier.** `/stock/candle` is premium, so
`get_daily_bars` falls back to synthesising one bar per symbol per day from
`/quote`. History therefore accumulates forward from the day you start rather
than being backfillable, and indicators with long windows (SMA200, the 52-week
range) stay empty until enough days have passed. `backfill_daily_bars` exists
and works — against the synthetic provider, or against Finnhub with
`finnhub_candles_enabled` and a key that allows it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import Phase, Position, UniverseMember
from app.db.upsert import upsert
from app.market import repository, universe
from app.market.indicators import compute_signals, frame_from_bars
from app.market.provider import BarData, ProviderError, QuoteData, get_provider
from app.market.screener import ScreenCriteria, screen
from app.market.universe import BENCHMARK_SYMBOL
from app.orchestrator.jobs import record_job

log = get_logger(__name__)
UTC = dt.timezone.utc

#: Enough history for a 200-day average and a 52-week range, with slack for
#: holidays and non-trading days.
SIGNAL_LOOKBACK_DAYS = 400

#: How many symbols are fetched concurrently. The provider's rate limiter sets
#: the real pace; this only stops us from opening 500 sockets at once.
FETCH_CONCURRENCY = 6

#: Symbols per database transaction. Small enough that a failure halfway
#: through a nine-minute pass does not discard the work already done.
WRITE_BATCH = 50


async def _gather_limited(coros: Sequence, concurrency: int = FETCH_CONCURRENCY) -> list:
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(guarded(c) for c in coros), return_exceptions=True)


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ----------------------------------------------------------------- universe


async def sync_universe_job(now: Optional[dt.datetime] = None) -> int:
    """Push the shipped S&P 500 CSV into `universe_members`. Idempotent."""
    async with record_job("universe.sync", now=now) as record:
        async with session_scope() as session:
            count = await universe.sync_universe(session)
        record.note(members=count)
        return count


# --------------------------------------------------------------- daily bars


async def ingest_daily_bars(
    symbols: Optional[Sequence[str]] = None,
    *,
    phase: Optional[Phase] = None,
    now: Optional[dt.datetime] = None,
    lookback_days: int = 1,
) -> dict[str, Any]:
    """Fetch and store daily bars for `symbols` (default: the whole universe)."""
    async with record_job("market.daily_bars", phase=phase, now=now) as record:
        provider = get_provider()
        async with session_scope() as session:
            targets = list(symbols) if symbols else await universe.active_symbols(session)

        end = (now or dt.datetime.now(UTC)).astimezone(UTC).date()
        start = end - dt.timedelta(days=max(lookback_days, 1))

        written = 0
        failures: list[str] = []
        for batch in _chunks(targets, WRITE_BATCH):
            results = await _gather_limited(
                [provider.get_daily_bars(symbol, start, end) for symbol in batch]
            )
            bars: list[BarData] = []
            for symbol, result in zip(batch, results, strict=True):
                if isinstance(result, Exception):
                    failures.append(symbol)
                    log.warning("ingest.bar_failed", symbol=symbol, error=str(result))
                    continue
                bars.extend(result)
            if bars:
                async with session_scope() as session:
                    written += await repository.upsert_bars(session, bars)

        record.note(symbols=len(targets), bars=written, failures=len(failures))
        if failures:
            record.note(failed_symbols=failures[:20])
        return {"symbols": len(targets), "bars": written, "failures": failures}


async def backfill_daily_bars(
    days: int = SIGNAL_LOOKBACK_DAYS,
    symbols: Optional[Sequence[str]] = None,
    *,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """One-off history load. See the module docstring on free-tier limits."""
    return await ingest_daily_bars(symbols, now=now, lookback_days=days)


# ------------------------------------------------------------------ signals


async def refresh_signals(
    symbols: Optional[Sequence[str]] = None,
    *,
    phase: Optional[Phase] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Recompute the screener's signal cache from stored bars.

    Runs right after a daily-bar ingest. Doing it here — once, in bulk — is what
    turns a screen into a single indexed query instead of 500 indicator
    computations on the critical path of a decision.
    """
    async with record_job("market.signals", phase=phase, now=now) as record:
        as_of = now
        computed = 0
        skipped = 0

        async with session_scope() as session:
            targets = list(symbols) if symbols else await universe.active_symbols(session)

        for batch in _chunks(targets, WRITE_BATCH):
            async with session_scope() as session:
                grouped = await repository.get_bars_bulk(
                    session,
                    batch,
                    lookback_days=SIGNAL_LOOKBACK_DAYS,
                    as_of=as_of,
                )
                rows = []
                stamp = as_of or dt.datetime.now(UTC)
                for symbol in batch:
                    bars = grouped.get(symbol) or []
                    signals = compute_signals(frame_from_bars(bars)) if bars else {}
                    if not signals:
                        skipped += 1
                        continue
                    rows.append(
                        {
                            "symbol": symbol,
                            "signals": signals,
                            "signals_updated_at": stamp,
                        }
                    )
                    computed += 1
                if rows:
                    await upsert(
                        session,
                        UniverseMember.__table__,
                        rows,
                        index_elements=[UniverseMember.symbol],
                        update_columns=["signals", "signals_updated_at"],
                    )

        record.note(computed=computed, skipped=skipped)
        return {"computed": computed, "skipped": skipped}


# ------------------------------------------------------------------- quotes


async def live_feed_symbols(session: AsyncSession) -> list[str]:
    """The active set: what is held, plus the day's best candidates.

    Capped at the provider's websocket limit, because this is the same set the
    live feed subscribes to. Held positions always win a slot — a portfolio must
    never be marked against a stale price — and candidates fill what is left.
    """
    held = set(
        (await session.execute(select(Position.symbol).where(Position.quantity != 0)))
        .scalars()
        .all()
    )
    active = {BENCHMARK_SYMBOL} | held

    slots = min(settings.live_feed_candidate_slots, settings.finnhub_ws_max_symbols - len(active))
    if slots > 0:
        candidates = await screen(session, ScreenCriteria(preset="volume", limit=slots))
        active.update(c.symbol for c in candidates)

    return sorted(active)[: settings.finnhub_ws_max_symbols]


async def ingest_quotes(
    symbols: Optional[Sequence[str]] = None,
    *,
    phase: Optional[Phase] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Poll quotes for the active set and store them."""
    async with record_job("market.quotes", phase=phase, now=now) as record:
        provider = get_provider()
        async with session_scope() as session:
            targets = list(symbols) if symbols else await live_feed_symbols(session)

        results = await _gather_limited([provider.get_quote(symbol) for symbol in targets])
        quotes: list[QuoteData] = []
        failures = 0
        for symbol, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                failures += 1
                log.warning("ingest.quote_failed", symbol=symbol, error=str(result))
                continue
            if result is not None:
                quotes.append(result)

        written = 0
        if quotes:
            async with session_scope() as session:
                written = await repository.record_quotes(session, quotes)

        record.note(symbols=len(targets), quotes=written, failures=failures)
        return {"symbols": len(targets), "quotes": written, "failures": failures}


# --------------------------------------------------------------------- news


async def ingest_news(
    symbols: Optional[Sequence[str]] = None,
    *,
    days: int = 2,
    phase: Optional[Phase] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Pull recent company news for the active set.

    Runs in `PRE_MARKET` so the overnight flow is in the store before the agents
    are asked to digest it, which is the one thing that phase is for.
    """
    async with record_job("market.news", phase=phase, now=now) as record:
        provider = get_provider()
        async with session_scope() as session:
            targets = list(symbols) if symbols else await live_feed_symbols(session)

        end = (now or dt.datetime.now(UTC)).astimezone(UTC).date()
        start = end - dt.timedelta(days=max(days, 1))

        results = await _gather_limited(
            [provider.get_news(symbol, start, end) for symbol in targets]
        )
        items = []
        failures = 0
        for symbol, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                failures += 1
                log.warning("ingest.news_failed", symbol=symbol, error=str(result))
                continue
            items.extend(result)

        written = 0
        if items:
            async with session_scope() as session:
                written = await repository.upsert_news(session, items)

        record.note(symbols=len(targets), items=written, failures=failures)
        return {"symbols": len(targets), "items": written, "failures": failures}


# ---------------------------------------------------------------- composites


async def daily_close_ingest(
    *, phase: Optional[Phase] = None, now: Optional[dt.datetime] = None
) -> dict[str, Any]:
    """The after-the-close pass: bars for the whole universe, then signals.

    Ordering matters — the signal cache is derived from the bars, so a failure
    in the bar pass must not leave the screener reading fresh signals computed
    from stale prices. Hence: bars first, and signals only if they arrived.
    """
    bars = await ingest_daily_bars(phase=phase, now=now)
    signals = await refresh_signals(phase=phase, now=now)
    return {"bars": bars, "signals": signals}


async def provider_health() -> dict[str, Any]:
    """Rate-limit consumption, for the health panel."""
    provider = get_provider()
    limiter = getattr(provider, "limiter", None)
    stats = limiter.stats() if limiter is not None else None
    return {
        "provider": getattr(provider, "name", "unknown"),
        "offline": settings.market_data_offline,
        "rate_limit": None
        if stats is None
        else {
            "limit_per_minute": stats.limit_per_minute,
            "used_last_minute": stats.used_last_minute,
            "remaining": stats.remaining,
            "total_requests": stats.total_requests,
            "total_throttled": stats.total_throttled,
            "utilisation": stats.utilisation,
        },
    }


__all__ = [
    "ProviderError",
    "backfill_daily_bars",
    "daily_close_ingest",
    "ingest_daily_bars",
    "ingest_news",
    "ingest_quotes",
    "live_feed_symbols",
    "provider_health",
    "refresh_signals",
    "sync_universe_job",
]
