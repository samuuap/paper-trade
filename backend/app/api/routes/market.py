"""Market data: the universe, the screener and per-symbol lookups.

These exist so the dashboard can show the same view of the market the agents
are given. If a candidate list looks wrong on screen, it is wrong in the
briefing too — and that is a much easier bug to find than "the model made a
strange choice".
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import UniverseMember
from app.market import repository
from app.market.screener import PRESETS, ScreenCriteria, describe_presets, screen
from app.market.universe import BENCHMARK_SYMBOL

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/presets")
async def list_presets() -> list[dict[str, str]]:
    """The screening presets available to agents and to this API."""
    return describe_presets()


@router.get("/universe")
async def list_universe(
    sector: Optional[str] = None,
    with_signals: bool = Query(False, description="Include the cached signal blob"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(UniverseMember).where(UniverseMember.is_active.is_(True))
    if sector:
        stmt = stmt.where(UniverseMember.sector == sector)
    rows = (await db.execute(stmt.order_by(UniverseMember.symbol))).scalars().all()
    return [
        {
            "symbol": r.symbol,
            "name": r.name,
            "sector": r.sector,
            "signals_updated_at": r.signals_updated_at.isoformat()
            if r.signals_updated_at
            else None,
            **({"signals": r.signals} if with_signals else {}),
        }
        for r in rows
    ]


@router.get("/screen")
async def run_screen(
    preset: str = Query("momentum"),
    limit: int = Query(15, ge=1, le=50),
    above_sma_50: Optional[bool] = None,
    above_sma_200: Optional[bool] = None,
    min_rsi: Optional[float] = None,
    max_rsi: Optional[float] = None,
    sectors: Optional[str] = Query(None, description="Comma-separated"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run a screen. Same code path the agents' `screen_stocks` tool uses."""
    if preset not in PRESETS:
        raise HTTPException(
            status_code=400, detail=f"unknown preset {preset!r}; known: {sorted(PRESETS)}"
        )
    criteria = ScreenCriteria(
        preset=preset,
        limit=limit,
        above_sma_50=above_sma_50,
        above_sma_200=above_sma_200,
        min_rsi=min_rsi,
        max_rsi=max_rsi,
        sectors=[s.strip() for s in sectors.split(",")] if sectors else [],
    )
    candidates = await screen(db, criteria)
    return {
        "preset": preset,
        "description": PRESETS[preset]["description"],
        "count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
    }


@router.get("/quote/{symbol}")
async def get_quote(symbol: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Latest stored quote. Never hits the provider — see the ingest layer."""
    row = await repository.latest_quote(db, symbol.upper())
    if row is None:
        raise HTTPException(status_code=404, detail=f"no quote stored for {symbol.upper()}")
    return {
        "symbol": row.symbol,
        "price": float(row.price),
        "prev_close": float(row.prev_close) if row.prev_close is not None else None,
        "day_open": float(row.day_open) if row.day_open is not None else None,
        "day_high": float(row.day_high) if row.day_high is not None else None,
        "day_low": float(row.day_low) if row.day_low is not None else None,
        "ts": row.ts.isoformat(),
        "source": row.source,
    }


@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str,
    limit: int = Query(120, ge=2, le=1000),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    bars = await repository.get_bars(db, symbol.upper(), limit=limit)
    return {
        "symbol": symbol.upper(),
        "bars": [
            {
                "ts": b.ts.isoformat(),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume or 0),
            }
            for b in bars
        ],
    }


@router.get("/news/{symbol}")
async def get_news(
    symbol: str,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    items = await repository.get_news(db, symbol.upper(), limit=limit)
    return [
        {
            "headline": n.headline,
            "summary": n.summary,
            "source": n.source,
            "url": n.url,
            "published_at": n.published_at.isoformat(),
        }
        for n in items
    ]


@router.get("/benchmark")
async def get_benchmark(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    member = (
        await db.execute(
            select(UniverseMember).where(UniverseMember.symbol == BENCHMARK_SYMBOL)
        )
    ).scalar_one_or_none()
    return {
        "symbol": BENCHMARK_SYMBOL,
        "signals": member.signals if member else {},
        "signals_updated_at": member.signals_updated_at.isoformat()
        if member and member.signals_updated_at
        else None,
    }
