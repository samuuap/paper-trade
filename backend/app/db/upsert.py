"""Dialect-aware upsert.

Postgres and SQLite both speak `ON CONFLICT`, but through different dialect
modules. This wrapper picks the right one so ingest code can be written once
and run against either — production on Postgres, tests and offline replays on
SQLite.

It also chunks. Both engines cap how many bound parameters a single statement
may carry, and a bulk insert is rows × columns parameters: a backfill of the
whole universe blows through either limit long before it is large enough to be
worth worrying about for any other reason. Chunking here means callers never
have to think about it, and cannot get it wrong for one table and right for
another.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator, Sequence

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

#: SQLite raised its parameter ceiling in 3.32; older builds cap at 999.
SQLITE_MAX_VARIABLES = 32_766 if sqlite3.sqlite_version_info >= (3, 32, 0) else 999
#: Postgres' wire protocol caps parameters at 65535 per statement.
POSTGRES_MAX_VARIABLES = 65_535
#: Headroom, so a table gaining a column does not turn into a runtime error.
SAFETY_MARGIN = 0.9


def _dialect(session: AsyncSession) -> str:
    return session.bind.dialect.name if session.bind is not None else "postgresql"


def _insert_for(session: AsyncSession):
    if _dialect(session) == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert


def chunk_size(dialect: str, columns: int) -> int:
    """How many rows of `columns` fields fit in one statement."""
    ceiling = SQLITE_MAX_VARIABLES if dialect == "sqlite" else POSTGRES_MAX_VARIABLES
    return max(1, int(ceiling * SAFETY_MARGIN) // max(columns, 1))


def _batches(rows: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


async def upsert(
    session: AsyncSession,
    table: Table,
    rows: Sequence[dict[str, Any]],
    index_elements: Sequence[Any],
    update_columns: Sequence[str] | None = None,
) -> int:
    """Insert `rows`, updating `update_columns` on conflict.

    Passing `update_columns=None` makes it a plain insert-or-ignore.
    """
    if not rows:
        return 0

    insert = _insert_for(session)
    # Row shapes within one call are uniform, so the first row sets the width.
    size = chunk_size(_dialect(session), len(rows[0]))

    written = 0
    for batch in _batches(rows, size):
        stmt = insert(table).values(list(batch))
        if update_columns:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(index_elements),
                set_={column: getattr(stmt.excluded, column) for column in update_columns},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(index_elements))
        await session.execute(stmt)
        written += len(batch)
    return written
