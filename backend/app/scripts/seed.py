"""Seed the roster.

Creates the six agents and their portfolios. Safe to run repeatedly: it is the
normal way to bring a fresh database, or a database that has gained a new
agent, up to date.

One deliberate asymmetry. Cosmetic fields (name, description, colour) are
refreshed from the code on every run, but **`strategy_prompt` and `config` are
not**. The stored prompt is the experiment's independent variable; once the
system is running it belongs to the database, and a seed run must never
silently reset an agent that was re-prompted on purpose. Pass `--force-prompts`
when resetting them is actually what you want.

    python -m app.scripts.seed                # create or top up
    python -m app.scripts.seed --force-prompts
    python -m app.scripts.seed --reset        # destructive: wipes agents first
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import default_roster
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.base import Base, get_engine, session_scope
from app.db.models import Agent, Portfolio
from app.market.universe import sync_universe

log = get_logger(__name__)


async def create_tables_if_sqlite() -> bool:
    """Tests and offline replays run on SQLite, which has no Alembic history."""
    if not settings.database_url.startswith("sqlite"):
        return False
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return True


async def seed_roster(
    session: AsyncSession, *, force_prompts: bool = False
) -> dict[str, Any]:
    created: list[str] = []
    updated: list[str] = []

    for entry in default_roster():
        agent = (
            await session.execute(select(Agent).where(Agent.slug == entry.slug))
        ).scalar_one_or_none()

        if agent is None:
            agent = Agent(
                slug=entry.slug,
                name=entry.name,
                kind=entry.kind,
                runtime=entry.runtime,
                description=entry.description,
                strategy_prompt=entry.strategy_prompt,
                config=entry.config,
                color=entry.color,
                is_active=True,
            )
            session.add(agent)
            await session.flush()
            created.append(entry.slug)
        else:
            agent.name = entry.name
            agent.description = entry.description
            agent.color = entry.color
            agent.runtime = entry.runtime
            if force_prompts:
                agent.strategy_prompt = entry.strategy_prompt
                agent.config = entry.config
            updated.append(entry.slug)

        portfolio = (
            await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
        ).scalar_one_or_none()
        if portfolio is None:
            capital = Decimal(str(settings.initial_capital))
            session.add(
                Portfolio(agent_id=agent.id, cash=capital, initial_capital=capital)
            )

    return {"created": created, "updated": updated}


async def reset_roster(session: AsyncSession) -> int:
    """Delete every agent. Cascades to portfolios, decisions, orders, fills."""
    count = len((await session.execute(select(Agent.id))).scalars().all())
    await session.execute(delete(Agent))
    return count


async def run(
    *, reset: bool = False, force_prompts: bool = False, with_universe: bool = True
) -> dict[str, Any]:
    configure_logging()
    created_tables = await create_tables_if_sqlite()

    async with session_scope() as session:
        removed = await reset_roster(session) if reset else 0
        result = await seed_roster(session, force_prompts=force_prompts)
        members = await sync_universe(session) if with_universe else 0

    summary = {
        "created_tables": created_tables,
        "removed": removed,
        "universe_members": members,
        **result,
    }
    log.info("seed.done", **summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the agent roster.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete existing agents first (destructive: takes their history with them)",
    )
    parser.add_argument(
        "--force-prompts",
        action="store_true",
        help="overwrite stored strategy prompts and config with the code defaults",
    )
    parser.add_argument(
        "--skip-universe", action="store_true", help="do not sync the S&P 500 CSV"
    )
    args = parser.parse_args()

    summary = asyncio.run(
        run(
            reset=args.reset,
            force_prompts=args.force_prompts,
            with_universe=not args.skip_universe,
        )
    )
    created = ", ".join(summary["created"]) or "none"
    print(f"created: {created}")
    print(f"updated: {', '.join(summary['updated']) or 'none'}")
    print(f"universe members: {summary['universe_members']}")


if __name__ == "__main__":
    main()
