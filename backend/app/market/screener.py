"""The screening layer.

Mandatory, not optional: 500 symbols do not fit in a local model's context, so
the agent's universe has to be narrowed before it ever sees it. The screener is
pure Python — filters, sorts and composite scores — and hands the model a short
list of candidates with the arithmetic already done.

Signals are read from the `universe_members.signals` cache, refreshed after each
daily-bar ingest, so a screen is a single indexed query rather than 500
indicator computations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UniverseMember
from app.market.universe import BENCHMARK_SYMBOL

#: What each preset optimises for, and which signals it needs present.
PRESETS: dict[str, dict[str, Any]] = {
    "momentum": {
        "sort_by": "momentum_score",
        "descending": True,
        "description": "Strongest trends: recent returns, trend alignment, volume confirmation.",
    },
    "value": {
        "sort_by": "value_score",
        "descending": True,
        "description": "Price-derived cheapness: depressed vs 52w range but still above its 200d.",
    },
    "reversion": {
        "sort_by": "reversion_score",
        "descending": True,
        "description": "Most stretched names, in either direction, versus their own history.",
    },
    "oversold": {
        "sort_by": "rsi_14",
        "descending": False,
        "description": "Lowest RSI first.",
    },
    "overbought": {
        "sort_by": "rsi_14",
        "descending": True,
        "description": "Highest RSI first.",
    },
    "volume": {
        "sort_by": "volume_ratio",
        "descending": True,
        "description": "Unusual volume versus the 20-day average.",
    },
    "gainers": {
        "sort_by": "change_pct",
        "descending": True,
        "description": "Biggest movers up on the session.",
    },
    "losers": {
        "sort_by": "change_pct",
        "descending": False,
        "description": "Biggest movers down on the session.",
    },
}

#: Compact payload handed to the model. Deliberately short — every extra field
#: is prompt tokens, and therefore latency, on every candidate.
CANDIDATE_FIELDS = (
    "price",
    "change_pct",
    "return_5d_pct",
    "return_21d_pct",
    "return_63d_pct",
    "rsi_14",
    "macd_hist",
    "above_sma_50",
    "above_sma_200",
    "dist_sma_50_pct",
    "pct_from_52w_high",
    "range_position_52w",
    "volatility_20d",
    "volume_ratio",
    "atr_pct",
    "momentum_score",
    "value_score",
    "reversion_score",
)

MAX_LIMIT = 50


@dataclass
class ScreenCriteria:
    preset: str = "momentum"
    limit: int = 15
    min_price: Optional[float] = 5.0
    max_price: Optional[float] = None
    min_dollar_volume: Optional[float] = 20_000_000.0
    min_rsi: Optional[float] = None
    max_rsi: Optional[float] = None
    above_sma_50: Optional[bool] = None
    above_sma_200: Optional[bool] = None
    min_change_pct: Optional[float] = None
    max_change_pct: Optional[float] = None
    sectors: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    exclude_symbols: list[str] = field(default_factory=list)

    def normalised(self) -> "ScreenCriteria":
        self.preset = (self.preset or "momentum").lower()
        if self.preset not in PRESETS:
            self.preset = "momentum"
        self.limit = max(1, min(int(self.limit or 15), MAX_LIMIT))
        return self


@dataclass
class Candidate:
    symbol: str
    name: str
    sector: str
    score: float
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "score": round(self.score, 4),
        }
        payload.update(self.signals)
        return payload


def _passes(signals: dict[str, Any], member: UniverseMember, c: ScreenCriteria) -> bool:
    if not signals:
        return False

    price = signals.get("price")
    if price is None:
        return False
    if c.min_price is not None and price < c.min_price:
        return False
    if c.max_price is not None and price > c.max_price:
        return False

    if c.min_dollar_volume is not None:
        dollar_volume = signals.get("dollar_volume")
        # Missing volume (e.g. bars assembled from quotes) should not silently
        # eliminate the whole universe, so treat unknown as "passes".
        if dollar_volume is not None and dollar_volume > 0 and dollar_volume < c.min_dollar_volume:
            return False

    rsi_value = signals.get("rsi_14")
    if c.min_rsi is not None and (rsi_value is None or rsi_value < c.min_rsi):
        return False
    if c.max_rsi is not None and (rsi_value is None or rsi_value > c.max_rsi):
        return False

    if c.above_sma_50 is not None and signals.get("above_sma_50") is not c.above_sma_50:
        return False
    if c.above_sma_200 is not None and signals.get("above_sma_200") is not c.above_sma_200:
        return False

    change = signals.get("change_pct")
    if c.min_change_pct is not None and (change is None or change < c.min_change_pct):
        return False
    if c.max_change_pct is not None and (change is None or change > c.max_change_pct):
        return False

    return not (c.sectors and member.sector not in c.sectors)


async def screen(session: AsyncSession, criteria: ScreenCriteria) -> list[Candidate]:
    criteria = criteria.normalised()
    preset = PRESETS[criteria.preset]
    sort_key = preset["sort_by"]
    descending = preset["descending"]

    stmt = select(UniverseMember).where(UniverseMember.is_active.is_(True))
    if criteria.symbols:
        stmt = stmt.where(UniverseMember.symbol.in_([s.upper() for s in criteria.symbols]))
    members = (await session.execute(stmt)).scalars().all()

    excluded = {s.upper() for s in criteria.exclude_symbols}
    # The benchmark is not a stock pick; it stays out of screens unless asked
    # for by name.
    if BENCHMARK_SYMBOL not in {s.upper() for s in criteria.symbols}:
        excluded.add(BENCHMARK_SYMBOL)

    candidates: list[Candidate] = []
    for member in members:
        if member.symbol in excluded:
            continue
        signals = member.signals or {}
        if not _passes(signals, member, criteria):
            continue
        score = signals.get(sort_key)
        if score is None:
            continue
        candidates.append(
            Candidate(
                symbol=member.symbol,
                name=member.name,
                sector=member.sector,
                score=float(score),
                signals={k: signals.get(k) for k in CANDIDATE_FIELDS if k in signals},
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=descending)
    return candidates[: criteria.limit]


def describe_presets() -> list[dict[str, str]]:
    return [
        {"name": name, "description": spec["description"]} for name, spec in PRESETS.items()
    ]
