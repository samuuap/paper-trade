"""Decisions, fills and the orders risk refused."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.metrics import queries

router = APIRouter(tags=["trading"])


@router.get("/decisions")
async def list_decisions(
    agent: Optional[str] = None,
    kind: Optional[str] = Query(None, description="TRADE | PLAN | REVIEW | RESEARCH"),
    status: Optional[str] = Query(None, description="OK | INVALID_OUTPUT | ERROR | SKIPPED"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Decision history with the full reasoning and instrumentation."""
    return await queries.decisions(
        db, slug=agent, kind=kind, status=status, limit=limit, offset=offset
    )


@router.get("/trades")
async def list_trades(
    agent: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Every fill, with the rationale that produced it and its execution costs."""
    return await queries.trade_log(db, slug=agent, symbol=symbol, limit=limit, offset=offset)


@router.get("/orders/rejected")
async def list_rejected(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Orders the deterministic risk layer refused, with the reason and the notes.

    The audit trail for "the model proposes, Python disposes": this is where you
    see what an agent wanted to do and why it was not allowed to.
    """
    return await queries.rejected_orders(db, limit=limit)
