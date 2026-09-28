"""Running one turn of the whole roster.

A cycle is the unit of work the phase machine schedules: every active agent
gets the same `cycle_id`, the same phase and — in a replay — the same `as_of`
instant, so their decisions are directly comparable. That comparability is the
entire experiment, so the things that could quietly break it are handled
explicitly here:

* **One transaction per agent.** An agent that throws must not roll back the
  decisions of the five that already succeeded.
* **Baselines run first.** They are deterministic and take milliseconds, so if
  inference is slow, wedged or short of memory, the reference curves are
  already recorded and the comparison survives.
* **A skipped decision is still a decision.** When the resource guard refuses
  inference, a `SKIPPED` row is written rather than nothing at all. Silence in
  the decisions table would be indistinguishable from the agent choosing to do
  nothing, and those are very different facts.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext, new_decision
from app.agents.registry import build_runtime
from app.core.events import bus
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import (
    Agent,
    AgentKind,
    Decision,
    DecisionKind,
    DecisionStatus,
    Phase,
    Portfolio,
)
from app.engine.execution import ExecutionEngine
from app.orchestrator import resources
from app.orchestrator.jobs import record_job

log = get_logger(__name__)
UTC = dt.timezone.utc

#: Which decision kind each phase asks for. The phase machine is the only
#: caller that needs this, but it belongs next to the cycle it describes.
PHASE_DECISION_KIND: dict[Phase, DecisionKind] = {
    Phase.PRE_MARKET: DecisionKind.PLAN,
    Phase.OPEN: DecisionKind.TRADE,
    Phase.AFTER_HOURS: DecisionKind.REVIEW,
    Phase.CLOSED: DecisionKind.RESEARCH,
}


@dataclass
class AgentOutcome:
    slug: str
    status: str
    decision_id: Optional[uuid.UUID] = None
    orders: int = 0
    filled: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "agent": self.slug,
            "status": self.status,
            "decision_id": str(self.decision_id) if self.decision_id else None,
            "orders": self.orders,
            "filled": self.filled,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass
class CycleResult:
    cycle_id: uuid.UUID
    kind: DecisionKind
    phase: Phase
    as_of: dt.datetime
    outcomes: list[AgentOutcome] = field(default_factory=list)

    @property
    def orders(self) -> int:
        return sum(o.orders for o in self.outcomes)

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    def to_dict(self) -> dict:
        return {
            "cycle_id": str(self.cycle_id),
            "kind": self.kind.value,
            "phase": self.phase.value,
            "as_of": self.as_of.isoformat(),
            "agents": len(self.outcomes),
            "orders": self.orders,
            "errors": self.errors,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


async def active_agents(session: AsyncSession) -> list[Agent]:
    """Active roster, baselines first (see the module docstring)."""
    rows = (
        await session.execute(select(Agent).where(Agent.is_active.is_(True)).order_by(Agent.slug))
    ).scalars().all()
    return sorted(rows, key=lambda a: (a.kind is not AgentKind.BASELINE, a.slug))


async def _load_pair(
    session: AsyncSession, agent_id: uuid.UUID
) -> tuple[Optional[Agent], Optional[Portfolio]]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return None, None
    portfolio = (
        await session.execute(select(Portfolio).where(Portfolio.agent_id == agent_id))
    ).scalar_one_or_none()
    return agent, portfolio


async def _record_skip(ctx: AgentContext, reason: str) -> Decision:
    decision = new_decision(ctx)
    decision.status = DecisionStatus.SKIPPED
    decision.error = reason
    ctx.session.add(decision)
    # Flushed so the row has its id: a skip that cannot be linked back to the
    # cycle that skipped it is barely better than no record at all.
    await ctx.session.flush()
    return decision


async def run_agent_turn(
    agent_id: uuid.UUID,
    *,
    kind: DecisionKind,
    phase: Phase,
    cycle_id: uuid.UUID,
    as_of: Optional[dt.datetime] = None,
    guard: Optional[resources.ResourceVerdict] = None,
) -> AgentOutcome:
    """One agent, one turn, one transaction."""
    clock = time.perf_counter()
    slug = str(agent_id)
    try:
        async with session_scope() as session:
            agent, portfolio = await _load_pair(session, agent_id)
            if agent is None or portfolio is None:
                return AgentOutcome(
                    slug=agent.slug if agent else slug,
                    status="error",
                    error="agent has no portfolio; run the seed script",
                )
            slug = agent.slug

            engine = ExecutionEngine(session, as_of=as_of)
            ctx = AgentContext(
                session=session,
                agent=agent,
                portfolio=portfolio,
                phase=phase,
                kind=kind,
                cycle_id=cycle_id,
                engine=engine,
                as_of=as_of,
            )

            # The guard only applies to agents that actually need the model.
            if agent.kind is AgentKind.LLM and guard is not None and not guard.allowed:
                decision = await _record_skip(ctx, guard.reason or "inference unavailable")
                await engine.snapshot_equity(agent, portfolio, phase)
                return AgentOutcome(
                    slug=slug,
                    status="skipped",
                    decision_id=decision.id,
                    duration_ms=(time.perf_counter() - clock) * 1000,
                    error=guard.reason,
                )

            runtime = build_runtime(agent)
            decision = await runtime.run(ctx)
            if decision is not None:
                # A runtime's error paths add the row without flushing, so the
                # id is only assigned here. The outcome reports the decision's
                # own status rather than "it returned something": an agent whose
                # model was unreachable has not had a successful turn.
                await ctx.session.flush()
            await engine.snapshot_equity(agent, portfolio, phase)

            filled = sum(1 for r in ctx.results if r.filled)
            return AgentOutcome(
                slug=slug,
                status=decision.status.value.lower() if decision is not None else "no_action",
                decision_id=decision.id if decision is not None else None,
                orders=len(ctx.results),
                filled=filled,
                duration_ms=(time.perf_counter() - clock) * 1000,
                error=decision.error if decision is not None else None,
            )
    except Exception as exc:  # noqa: BLE001 - one agent must not sink the cycle
        log.exception("cycle.agent_failed", agent=slug, kind=kind.value)
        return AgentOutcome(
            slug=slug,
            status="error",
            duration_ms=(time.perf_counter() - clock) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


async def run_cycle(
    kind: DecisionKind,
    phase: Phase,
    *,
    as_of: Optional[dt.datetime] = None,
    only: Optional[Sequence[str]] = None,
) -> CycleResult:
    """Run `kind` for every active agent. `only` restricts it to some slugs."""
    cycle_id = uuid.uuid4()
    now = as_of or dt.datetime.now(UTC)
    result = CycleResult(cycle_id=cycle_id, kind=kind, phase=phase, as_of=now)

    async with record_job(f"cycle.{kind.value.lower()}", phase=phase, now=as_of) as record:
        async with session_scope() as session:
            roster = await active_agents(session)
            if only:
                wanted = {s.lower() for s in only}
                roster = [a for a in roster if a.slug.lower() in wanted]
            targets = [(a.id, a.slug, a.kind) for a in roster]

        if not targets:
            log.warning("cycle.empty_roster", kind=kind.value)
            record.note(agents=0)
            return result

        # Evaluated once for the whole cycle: memory does not meaningfully move
        # between two agents, and re-reading it per agent would be noise.
        guard = resources.check_inference() if any(k is AgentKind.LLM for _, _, k in targets) else None
        if guard is not None and not guard.allowed:
            log.warning("cycle.inference_vetoed", reason=guard.reason)

        bus.publish(
            "cycle",
            status="started",
            cycle_id=str(cycle_id),
            kind=kind.value,
            phase=phase.value,
            agents=len(targets),
        )

        for agent_id, _slug, _kind in targets:
            outcome = await run_agent_turn(
                agent_id,
                kind=kind,
                phase=phase,
                cycle_id=cycle_id,
                as_of=as_of,
                guard=guard,
            )
            result.outcomes.append(outcome)

        record.note(
            agents=len(result.outcomes),
            orders=result.orders,
            errors=result.errors,
            skipped=sum(1 for o in result.outcomes if o.status == "skipped"),
        )

    bus.publish(
        "cycle",
        status="finished",
        cycle_id=str(cycle_id),
        kind=kind.value,
        phase=phase.value,
        agents=len(result.outcomes),
        orders=result.orders,
        errors=result.errors,
    )
    log.info(
        "cycle.finished",
        kind=kind.value,
        phase=phase.value,
        agents=len(result.outcomes),
        orders=result.orders,
        errors=result.errors,
    )
    return result


async def snapshot_all_equity(
    phase: Phase, *, as_of: Optional[dt.datetime] = None
) -> int:
    """Mark every portfolio to market. Drives the equity curve between cycles."""
    written = 0
    async with session_scope() as session:
        roster = await active_agents(session)
        engine = ExecutionEngine(session, as_of=as_of)
        for agent in roster:
            portfolio = (
                await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
            ).scalar_one_or_none()
            if portfolio is None:
                continue
            snapshot = await engine.snapshot_equity(agent, portfolio, phase)
            written += 1
            bus.publish(
                "equity",
                agent=agent.slug,
                agent_id=str(agent.id),
                ts=snapshot.ts.isoformat(),
                equity=float(snapshot.equity),
                cash=float(snapshot.cash),
                positions_value=float(snapshot.positions_value),
                unrealized_pnl=float(snapshot.unrealized_pnl),
            )
    return written


__all__ = [
    "PHASE_DECISION_KIND",
    "AgentOutcome",
    "CycleResult",
    "active_agents",
    "run_agent_turn",
    "run_cycle",
    "snapshot_all_equity",
]
