"""The tradable universe: the S&P 500, plus the benchmark ETF.

The constituent list ships with the repo as a CSV so the system boots without a
network call. `sync_universe` pushes it into `universe_members`, which is the
table everything else reads from.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import UniverseMember
from app.db.upsert import upsert

log = get_logger(__name__)

#: Everything is measured against this. Also the instrument the Buy & Hold
#: baseline owns, which makes it a permanent sanity check on the whole engine.
BENCHMARK_SYMBOL = "SPY"


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    name: str
    sector: str


def _csv_path() -> Path:
    path = Path(settings.universe_file)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / settings.universe_file


def load_universe_file() -> list[UniverseEntry]:
    path = _csv_path()
    if not path.exists():
        raise FileNotFoundError(f"universe file not found: {path}")
    entries: list[UniverseEntry] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            entries.append(
                UniverseEntry(
                    symbol=symbol,
                    name=(row.get("name") or "").strip(),
                    sector=(row.get("sector") or "").strip(),
                )
            )
    return entries


async def sync_universe(session: AsyncSession) -> int:
    """Upsert the CSV into `universe_members`. Idempotent."""
    entries = load_universe_file()
    entries.append(UniverseEntry(BENCHMARK_SYMBOL, "SPDR S&P 500 ETF Trust", "Benchmark"))

    rows = [
        {"symbol": e.symbol, "name": e.name, "sector": e.sector, "is_active": True}
        for e in entries
    ]
    await upsert(
        session,
        UniverseMember.__table__,
        rows,
        index_elements=[UniverseMember.symbol],
        update_columns=["name", "sector", "is_active"],
    )
    log.info("universe.synced", count=len(rows))
    return len(rows)


async def active_symbols(session: AsyncSession, include_benchmark: bool = True) -> list[str]:
    stmt = select(UniverseMember.symbol).where(UniverseMember.is_active.is_(True))
    result = await session.execute(stmt)
    symbols = sorted(result.scalars().all())
    if not include_benchmark:
        symbols = [s for s in symbols if s != BENCHMARK_SYMBOL]
    return symbols


async def sector_map(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(UniverseMember.symbol, UniverseMember.sector))
    return {symbol: sector for symbol, sector in result.all()}


async def get_member(session: AsyncSession, symbol: str) -> Optional[UniverseMember]:
    return await session.get(UniverseMember, symbol.upper())
