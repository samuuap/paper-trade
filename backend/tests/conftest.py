"""Test fixtures: a real database, a fake market, no network.

The suite runs against SQLite rather than Postgres. That is a deliberate
trade-off: it costs fidelity on the Timescale-specific bits (hypertables,
compression), which are not what these tests are checking, and it buys a suite
that runs anywhere in a couple of seconds with no service to start. The
dialect-aware `upsert` helper exists precisely so the same ingest code works
against both.

The market is the deterministic `SyntheticProvider`: same symbol and same day
always produce the same prices, so a failing assertion is a real regression
rather than a different random walk.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.events import bus
from app.db.base import Base, configure_engine, dispose_engine, session_scope
from app.db.models import Agent, MarketQuote, UniverseMember
from app.db.upsert import upsert
from app.market.provider import SyntheticProvider, set_provider
from app.orchestrator import ingest
from app.scripts import seed

#: Small enough to keep the suite fast, varied enough that screens return
#: something and the momentum/value presets do not collapse to one name.
TEST_SYMBOLS = [
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "JPM", "XOM", "JNJ", "PG", "KO", "WMT",
]

BAR_HISTORY_DAYS = 400


@pytest_asyncio.fixture(scope="session")
async def engine(tmp_path_factory):
    """One file-backed SQLite database for the whole run.

    A file rather than `:memory:` because several code paths open their own
    session (`record_job` writes its trace outside the job's transaction on
    purpose), and an in-memory database is per-connection.
    """
    path = tmp_path_factory.mktemp("db") / "test.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    # Registered before the first connection is opened: the `PRAGMA
    # foreign_keys=ON` hook only fires on connect, and a pooled connection
    # created by `create_all` would otherwise never get it — leaving deletes
    # un-cascaded and letting one test's agents leak into the next.
    configure_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


@pytest_asyncio.fixture(scope="session")
async def market(engine):
    """Universe, daily bars, signals and quotes for `TEST_SYMBOLS`."""
    set_provider(SyntheticProvider(seed="tests"))

    async with session_scope() as session:
        await upsert(
            session,
            UniverseMember.__table__,
            [
                {"symbol": s, "name": f"{s} Inc.", "sector": "Test", "is_active": True}
                for s in TEST_SYMBOLS
            ],
            index_elements=[UniverseMember.symbol],
            update_columns=["name", "sector", "is_active"],
        )

    await ingest.backfill_daily_bars(days=BAR_HISTORY_DAYS, symbols=TEST_SYMBOLS)
    await ingest.refresh_signals(symbols=TEST_SYMBOLS)
    await ingest.ingest_quotes(symbols=TEST_SYMBOLS)
    yield TEST_SYMBOLS
    set_provider(None)


@pytest_asyncio.fixture
async def roster(market):
    """A freshly seeded roster for each test.

    Deleting the agents cascades to portfolios, decisions, orders and fills, so
    every test starts from six untouched portfolios without rebuilding the
    market data around them.
    """
    async with session_scope() as session:
        await session.execute(delete(Agent))
    await seed.run(with_universe=False)
    yield
    async with session_scope() as session:
        await session.execute(delete(Agent))


@pytest_asyncio.fixture
async def db(engine):
    """A session for assertions. Does not commit."""
    from app.db.base import get_session_factory

    async with get_session_factory()() as session:
        yield session


@pytest.fixture
def today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


@pytest_asyncio.fixture
async def fresh_quotes(market):
    """Re-record quotes at the current instant.

    Some tests move the clock around; reference prices fall back to the last
    daily bar when no quote is recent, and this keeps that path predictable.
    """
    async with session_scope() as session:
        await session.execute(delete(MarketQuote))
    await ingest.ingest_quotes(symbols=TEST_SYMBOLS)


@pytest.fixture(autouse=True)
def quiet_bus():
    """Start every test with an empty replay buffer.

    The event bus is a module-level singleton, so without this an assertion
    about "the most recent events" silently depends on which test ran before.
    """
    bus.clear()
    yield
    bus.clear()
