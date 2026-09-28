"""SQLAlchemy engine, session factory and declarative base.

The engine is created lazily. Importing a module that happens to touch
`app.db` should never require a database driver to be installed or a server to
be reachable — that keeps the calendar, indicators and engine maths importable
(and testable) on their own.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Optional

from sqlalchemy import DateTime, MetaData, Numeric, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Money and share quantities are stored as fixed-point, never float. Rounding
#: drift in a ledger is the kind of bug that silently corrupts every metric
#: downstream.
Money = Numeric(20, 6)
Quantity = Numeric(20, 6)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Defaults for the two types we never want guessed. JSONB columns always
    #: declare their type explicitly at the column.
    type_annotation_map = {
        Decimal: Money,
        datetime: DateTime(timezone=True),
    }


_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _enforce_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """SQLite ignores `ON DELETE CASCADE` unless foreign keys are switched on.

    Off by default, per connection. Without this, retiring an agent on the
    SQLite path leaves its portfolio, decisions, orders and fills behind as
    orphans — which is silent, and which makes a test suite or a replay quietly
    accumulate another run's data.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        kwargs: dict = {"echo": settings.db_echo, "pool_pre_ping": True}
        # SQLite (used by tests) has no pooling knobs worth setting.
        if not settings.database_url.startswith("sqlite"):
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
        _engine = create_async_engine(settings.database_url, **kwargs)
        if settings.database_url.startswith("sqlite"):
            _enforce_sqlite_foreign_keys(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _session_factory


def configure_engine(engine: AsyncEngine) -> None:
    """Point the app at a different engine (tests, replay sandboxes)."""
    global _engine, _session_factory
    if engine.url.get_backend_name() == "sqlite":
        _enforce_sqlite_foreign_keys(engine)
    _engine = engine
    _session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background jobs (the API uses `deps.get_db`)."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
