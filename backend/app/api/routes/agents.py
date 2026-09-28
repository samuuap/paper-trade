"""Agents, the leaderboard and the equity curves."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import Agent
from app.metrics import queries

router = APIRouter(tags=["agents"])
UTC = dt.timezone.utc


@router.get("/agents")
async def list_agents(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """The roster, without the heavy per-agent computation."""
    rows = (await db.execute(select(Agent).order_by(Agent.slug))).scalars().all()
    return [
        {
            "id": str(a.id),
            "slug": a.slug,
            "name": a.name,
            "kind": a.kind.value,
            "runtime": a.runtime,
            "description": a.description,
            "color": a.color,
            "model": a.model,
            "is_active": a.is_active,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.get("/leaderboard")
async def get_leaderboard(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Ranking by return, with risk metrics and what the decisions cost.

    Equity is marked to market on every request, so the ordering moves during
    the session rather than only at the daily rollup.
    """
    return await queries.leaderboard(db)


@router.get("/equity")
async def get_equity_curves(
    since_hours: Optional[float] = Query(None, ge=0, description="Only points newer than this"),
    agents: Optional[str] = Query(None, description="Comma-separated slugs"),
    max_points: int = Query(1500, ge=50, le=20000),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Equity series per agent plus the benchmark, rebased to the same capital."""
    since = (
        dt.datetime.now(UTC) - dt.timedelta(hours=since_hours)
        if since_hours is not None
        else None
    )
    slugs = [s.strip() for s in agents.split(",") if s.strip()] if agents else None
    return await queries.equity_curves(db, since=since, slugs=slugs, max_points=max_points)


@router.get("/agents/{slug}")
async def get_agent(slug: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """One agent: its portfolio, its open positions and its daily history."""
    detail = await queries.agent_detail(db, slug)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {slug!r}")
    return detail


@router.get("/economics")
async def get_economics(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Token spend, latency distribution, shadow cost and seconds per P&L dollar."""
    return await queries.agent_economics(db)
