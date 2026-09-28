"""Market-data providers.

One interface, two implementations:

* `FinnhubProvider` — the real thing, rate-limited.
* `SyntheticProvider` — deterministic random walks, seeded per symbol. Lets the
  whole system be exercised (replay runs, tests, a laptop on a plane) without an
  API key and without burning request budget.

Everything ingested lands in the database; agents never talk to a provider
directly. That decoupling is what makes cycles reproducible.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.market.rate_limiter import RateLimiter

log = get_logger(__name__)
UTC = dt.timezone.utc


@dataclass
class QuoteData:
    symbol: str
    price: Decimal
    prev_close: Optional[Decimal]
    day_open: Optional[Decimal]
    day_high: Optional[Decimal]
    day_low: Optional[Decimal]
    ts: dt.datetime
    source: str = "finnhub"


@dataclass
class BarData:
    symbol: str
    ts: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe: str = "1D"
    source: str = "finnhub"


@dataclass
class NewsData:
    symbol: str
    external_id: str
    headline: str
    summary: str
    source: str
    url: str
    published_at: dt.datetime


class ProviderError(RuntimeError):
    pass


class MarketDataProvider(Protocol):
    name: str

    async def get_quote(self, symbol: str) -> Optional[QuoteData]: ...

    async def get_daily_bars(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> list[BarData]: ...

    async def get_news(self, symbol: str, start: dt.date, end: dt.date) -> list[NewsData]: ...

    async def aclose(self) -> None: ...


def _dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


# ------------------------------------------------------------------ finnhub


class FinnhubProvider:
    """Finnhub REST client.

    Note on daily bars: `/stock/candle` is a premium endpoint. On the free tier
    we assemble the daily bar from `/quote`, which returns the session's open,
    high, low, close and previous close in a single free call. That is one
    request per symbol — ~9 minutes for the S&P 500 at 55 req/min, which is
    nothing during AFTER_HOURS. Set `finnhub_candles_enabled` if your key has
    candle access and you want real history backfill.
    """

    name = "finnhub"

    def __init__(self, api_key: Optional[str] = None, limiter: Optional[RateLimiter] = None):
        self.api_key = api_key if api_key is not None else settings.finnhub_api_key
        self.limiter = limiter or RateLimiter(settings.finnhub_rate_limit_per_minute)
        self._client = httpx.AsyncClient(
            base_url=settings.finnhub_rest_url,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"X-Finnhub-Token": self.api_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> Optional[dict]:
        await self.limiter.acquire()
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"finnhub request failed: {exc}") from exc

        if response.status_code == 429:
            # Should not happen given the limiter, but if the account is shared
            # we back off rather than hammer.
            raise ProviderError("finnhub rate limit hit (429)")
        if response.status_code == 403:
            raise ProviderError(f"finnhub denied access to {path} (premium endpoint?)")
        if response.status_code >= 400:
            raise ProviderError(f"finnhub {response.status_code} on {path}: {response.text[:200]}")
        return response.json()

    async def get_quote(self, symbol: str) -> Optional[QuoteData]:
        payload = await self._get("/quote", {"symbol": symbol})
        if not payload or not payload.get("c"):
            return None
        ts = payload.get("t")
        return QuoteData(
            symbol=symbol,
            price=_dec(payload["c"]) or Decimal("0"),
            prev_close=_dec(payload.get("pc")),
            day_open=_dec(payload.get("o")),
            day_high=_dec(payload.get("h")),
            day_low=_dec(payload.get("l")),
            ts=dt.datetime.fromtimestamp(ts, tz=UTC) if ts else dt.datetime.now(UTC),
            source=self.name,
        )

    async def get_daily_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[BarData]:
        if settings.finnhub_candles_enabled:
            return await self._candles(symbol, start, end)
        quote = await self.get_quote(symbol)
        if quote is None or not quote.day_open:
            return []
        return [
            BarData(
                symbol=symbol,
                ts=dt.datetime.combine(quote.ts.astimezone(UTC).date(), dt.time(0), tzinfo=UTC),
                open=quote.day_open,
                high=quote.day_high or quote.price,
                low=quote.day_low or quote.price,
                close=quote.price,
                volume=Decimal("0"),
                source=f"{self.name}:quote",
            )
        ]

    async def _candles(self, symbol: str, start: dt.date, end: dt.date) -> list[BarData]:
        payload = await self._get(
            "/stock/candle",
            {
                "symbol": symbol,
                "resolution": "D",
                "from": int(dt.datetime.combine(start, dt.time(0), tzinfo=UTC).timestamp()),
                "to": int(dt.datetime.combine(end, dt.time(23, 59), tzinfo=UTC).timestamp()),
            },
        )
        if not payload or payload.get("s") != "ok":
            return []
        bars = []
        for i, epoch in enumerate(payload["t"]):
            bars.append(
                BarData(
                    symbol=symbol,
                    ts=dt.datetime.fromtimestamp(epoch, tz=UTC),
                    open=_dec(payload["o"][i]) or Decimal("0"),
                    high=_dec(payload["h"][i]) or Decimal("0"),
                    low=_dec(payload["l"][i]) or Decimal("0"),
                    close=_dec(payload["c"][i]) or Decimal("0"),
                    volume=_dec(payload["v"][i]) or Decimal("0"),
                    source=self.name,
                )
            )
        return bars

    async def get_news(self, symbol: str, start: dt.date, end: dt.date) -> list[NewsData]:
        payload = await self._get(
            "/company-news",
            {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
        )
        if not isinstance(payload, list):
            return []
        items = []
        for row in payload[:40]:
            published = row.get("datetime")
            items.append(
                NewsData(
                    symbol=symbol,
                    external_id=str(row.get("id") or row.get("url", ""))[:64],
                    headline=(row.get("headline") or "").strip(),
                    summary=(row.get("summary") or "").strip()[:2000],
                    source=(row.get("source") or "")[:64],
                    url=row.get("url") or "",
                    published_at=dt.datetime.fromtimestamp(published, tz=UTC)
                    if published
                    else dt.datetime.now(UTC),
                )
            )
        return [i for i in items if i.headline]


# ---------------------------------------------------------------- synthetic


#: The synthetic walk is anchored here so a symbol's price on a given date is
#: the same number no matter which window asked for it.
SYNTHETIC_EPOCH = dt.date(2020, 1, 1)


class SyntheticProvider:
    """Deterministic fake market. Same symbol + same day => same numbers.

    Prices follow a seeded geometric random walk with a per-symbol drift and
    volatility, so screeners and indicators see something with actual structure
    rather than noise.

    The walk runs from a fixed epoch rather than from the start of whatever
    window was requested. That matters more than it sounds: with a
    window-relative walk, a quote and a bar for the same day disagreed, so an
    offline run showed a portfolio jumping the moment it was marked against a
    live price instead of its own history — which looks exactly like an engine
    bug and is not one. Anchoring the walk makes bars and quotes two views of
    one price series.
    """

    name = "synthetic"

    def __init__(self, seed: str = "paper-trade") -> None:
        self.seed = seed
        self._limiter = RateLimiter(10_000)
        self._walks: dict[str, list[float]] = {}

    async def aclose(self) -> None:
        return None

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def _params(self, symbol: str) -> tuple[float, float, float]:
        digest = hashlib.sha256(f"{self.seed}:{symbol}".encode()).digest()
        base = 20 + (int.from_bytes(digest[0:4], "big") % 48_000) / 100  # $20 - $500
        # Daily drift, deliberately small: it now compounds over the whole
        # epoch window rather than over whatever window was requested, and the
        # old magnitude turned a $40 stock into a $10,000 one by 2026.
        drift = ((int.from_bytes(digest[4:6], "big") % 2000) - 900) / 5_000_000
        vol = 0.008 + (int.from_bytes(digest[6:8], "big") % 300) / 10_000  # 0.8% - 3.8%
        return base, drift, vol

    def _walk(self, symbol: str, through: dt.date) -> list[float]:
        """Daily closes from the epoch to `through`, memoised and extended.

        Indexed by calendar day offset from `SYNTHETIC_EPOCH`, so
        `walk[(day - SYNTHETIC_EPOCH).days]` is that day's close.
        """
        needed = (through - SYNTHETIC_EPOCH).days + 1
        series = self._walks.get(symbol)
        if series is not None and len(series) >= needed:
            return series

        base, drift, vol = self._params(symbol)
        rng = random.Random(f"{self.seed}:{symbol}:walk")
        price = base
        series = []
        for _ in range(max(needed, 1)):
            series.append(price)
            price = max(1.0, price * math.exp(rng.gauss(drift, vol)))
        self._walks[symbol] = series
        return series

    def close_on(self, symbol: str, day: dt.date) -> float:
        """The one true synthetic close for `symbol` on `day`."""
        if day < SYNTHETIC_EPOCH:
            day = SYNTHETIC_EPOCH
        return self._walk(symbol, day)[(day - SYNTHETIC_EPOCH).days]

    async def get_quote(self, symbol: str) -> Optional[QuoteData]:
        today = dt.datetime.now(UTC).date()
        prev = self.close_on(symbol, today - dt.timedelta(days=1))
        last = self.close_on(symbol, today)
        rng = random.Random(f"{self.seed}:{symbol}:{today}:intraday")
        high = last * (1 + abs(rng.gauss(0, 0.004)))
        low = last * (1 - abs(rng.gauss(0, 0.004)))
        return QuoteData(
            symbol=symbol,
            price=Decimal(f"{last:.2f}"),
            prev_close=Decimal(f"{prev:.2f}"),
            day_open=Decimal(f"{(prev + last) / 2:.2f}"),
            day_high=Decimal(f"{high:.2f}"),
            day_low=Decimal(f"{low:.2f}"),
            ts=dt.datetime.now(UTC),
            source=self.name,
        )

    async def get_daily_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[BarData]:
        from app.market.calendar import market_calendar

        days = market_calendar.trading_days(start, end)
        if not days:
            return []
        closes = [self.close_on(symbol, day) for day in days]
        _, _, vol = self._params(symbol)
        rng = random.Random(f"{self.seed}:{symbol}:bars")
        bars = []
        prev_close = closes[0]
        for day, close in zip(days, closes, strict=True):
            open_ = prev_close * (1 + rng.gauss(0, vol / 3))
            high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
            volume = int(abs(rng.gauss(4_000_000, 1_500_000))) + 100_000
            bars.append(
                BarData(
                    symbol=symbol,
                    ts=dt.datetime.combine(day, dt.time(0), tzinfo=UTC),
                    open=Decimal(f"{open_:.2f}"),
                    high=Decimal(f"{high:.2f}"),
                    low=Decimal(f"{low:.2f}"),
                    close=Decimal(f"{close:.2f}"),
                    volume=Decimal(volume),
                    source=self.name,
                )
            )
            prev_close = close
        return bars

    async def get_news(self, symbol: str, start: dt.date, end: dt.date) -> list[NewsData]:
        rng = random.Random(f"{self.seed}:{symbol}:news:{end.isoformat()}")
        templates = [
            ("{s} beats quarterly estimates", "Revenue came in ahead of consensus."),
            ("{s} announces buyback programme", "Board authorised a new repurchase."),
            ("Analysts cut price target on {s}", "Citing margin pressure in the near term."),
            ("{s} expands into new market", "Management flagged a multi-year opportunity."),
            ("{s} shares slip after guidance", "Forward guidance landed below expectations."),
        ]
        out = []
        for i in range(rng.randint(0, 3)):
            headline, summary = templates[rng.randrange(len(templates))]
            out.append(
                NewsData(
                    symbol=symbol,
                    external_id=f"syn-{symbol}-{end.isoformat()}-{i}",
                    headline=headline.format(s=symbol),
                    summary=summary,
                    source="synthetic-wire",
                    url=f"https://example.invalid/{symbol}/{i}",
                    published_at=dt.datetime.now(UTC) - dt.timedelta(hours=rng.randint(1, 36)),
                )
            )
        return out


_provider: Optional[MarketDataProvider] = None


def get_provider() -> MarketDataProvider:
    global _provider
    if _provider is None:
        if settings.market_data_offline or not settings.finnhub_api_key:
            if not settings.market_data_offline:
                log.warning("provider.no_api_key", detail="falling back to synthetic market data")
            _provider = SyntheticProvider()
        else:
            _provider = FinnhubProvider()
        log.info("provider.selected", provider=_provider.name)
    return _provider


def set_provider(provider: Optional[MarketDataProvider]) -> None:
    """Test/replay hook."""
    global _provider
    _provider = provider
