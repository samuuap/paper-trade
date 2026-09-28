"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """Read-oriented session for request handlers.

    Background jobs use `session_scope`, which commits on exit. Requests do not
    commit: the API is a read surface, and a handler that writes should say so
    explicitly rather than inherit it from its session.
    """
    async with get_session_factory()() as session:
        yield session
