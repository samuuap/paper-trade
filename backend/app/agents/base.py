"""Shared agent runtime scaffolding.

`AgentContext` is the single object handed to tools, runtimes and baselines. It
carries the database session, the agent's own portfolio, the current phase and —
critically — `as_of`, which pins every read to a simulated instant so a replay
reproduces a past session exactly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, Decision, DecisionKind, Phase, Portfolio
from app.engine.execution import ExecutionEngine, ExecutionResult
from app.engine.risk import OrderIntent, RiskContext, RiskLimits

UTC = dt.timezone.utc


@dataclass
class AgentContext:
    session: AsyncSession
    agent: Agent
    portfolio: Portfolio
    phase: Phase
    kind: DecisionKind
    cycle_id: uuid.UUID
    engine: ExecutionEngine
    as_of: Optional[dt.datetime] = None
    #: Accumulates the orders submitted during this turn.
    results: list[ExecutionResult] = field(default_factory=list)
    _risk_context: Optional[RiskContext] = None

    @property
    def now(self) -> dt.datetime:
        return self.as_of or dt.datetime.now(UTC)

    async def held_symbols(self) -> list[str]:
        positions = await self.engine.load_positions(self.portfolio.id)
        return sorted(positions.keys())

    @property
    def limits(self) -> RiskLimits:
        return RiskLimits.for_agent(self.agent.config)

    async def risk_context(self, extra_symbols: Optional[list[str]] = None) -> RiskContext:
        if self._risk_context is None:
            self._risk_context = await self.engine.build_context(
                self.portfolio, extra_symbols=extra_symbols or [], limits=self.limits
            )
        return self._risk_context

    def invalidate_risk_context(self) -> None:
        self._risk_context = None

    async def submit_order(
        self, intent: OrderIntent, decision_id: Optional[uuid.UUID] = None
    ) -> ExecutionResult:
        context = await self.risk_context([intent.symbol])
        result = await self.engine.submit(
            self.agent, self.portfolio, intent, context, decision_id
        )
        self.results.append(result)
        if result.filled:
            self.invalidate_risk_context()
        return result


class AgentRuntime(Protocol):
    """What every agent — LLM or baseline — must implement."""

    name: str

    async def run(self, ctx: AgentContext) -> Optional[Decision]: ...


def new_decision(
    ctx: AgentContext,
    *,
    model: Optional[str] = None,
    **overrides: Any,
) -> Decision:
    """Pre-populated `Decision` row, so every runtime records the same fields."""
    decision = Decision(
        agent_id=ctx.agent.id,
        cycle_id=ctx.cycle_id,
        phase=ctx.phase,
        kind=ctx.kind,
        model=model,
        created_at=ctx.now,
    )
    for key, value in overrides.items():
        setattr(decision, key, value)
    return decision
