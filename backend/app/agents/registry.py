"""The roster, and how a database row becomes a running agent.

Two things live here, and they are deliberately in the same place:

* **`DEFAULT_ROSTER`** — the six competitors the system ships with. Four LLM
  agents that differ *only* in their strategy prompt, plus two baselines. The
  seed script writes these into `agents`; nothing else reads them at runtime.
* **`build_runtime`** — the dispatch from `agents.runtime` to the object that
  actually runs a turn.

The split matters because the database is the source of truth once the system
is running: adding, retiring or re-prompting an agent is a row change, not a
deploy. In particular the strategy prompt stored on the row wins over the one
in `strategies.py`, so an experiment can vary the independent variable without
touching code. The code definitions are the defaults used at seed time.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from app.agents.base import AgentRuntime
from app.agents.baselines import BuyAndHoldRuntime, QuantSmaRuntime
from app.agents.llm_agent import LLMAgentRuntime
from app.agents.strategies import STRATEGIES, get_strategy
from app.db.models import Agent, AgentKind
from app.market.universe import BENCHMARK_SYMBOL

RUNTIME_LLM = "llm"
RUNTIME_BUY_AND_HOLD = "buy_and_hold"
RUNTIME_QUANT_SMA = "quant_sma"


@dataclass(frozen=True)
class RosterEntry:
    """A seedable agent definition."""

    slug: str
    name: str
    kind: AgentKind
    runtime: str
    description: str
    color: str
    strategy_prompt: str = ""
    config: dict[str, Any] = field(default_factory=dict)


def _llm_entries() -> list[RosterEntry]:
    return [
        RosterEntry(
            slug=strategy.slug,
            name=strategy.name,
            kind=AgentKind.LLM,
            runtime=RUNTIME_LLM,
            description=strategy.description,
            color=strategy.color,
            strategy_prompt=strategy.prompt,
        )
        for strategy in STRATEGIES.values()
    ]


#: Buy & Hold has to be 100% in one instrument or its return stops tracking the
#: index, which is the only thing it exists to do. The global 20% position cap
#: and the cash buffer are therefore both overridden for it.
_BUY_AND_HOLD_CONFIG: dict[str, Any] = {
    "symbol": BENCHMARK_SYMBOL,
    "risk": {
        "max_position_pct": 1.0,
        "max_gross_exposure_pct": 1.0,
        "cash_buffer_pct": 0.0,
        "max_orders_per_cycle": 1,
    },
}

#: Five equal-weight slots. The position cap is set a little above 20% so that
#: rounding never stops the fifth slot from being filled.
_QUANT_SMA_CONFIG: dict[str, Any] = {
    "max_positions": 5,
    "risk": {
        "max_position_pct": 0.25,
        "max_orders_per_cycle": 10,
    },
}


def default_roster() -> list[RosterEntry]:
    """The six agents the system ships with: 4 LLM + 2 baselines."""
    return _llm_entries() + [
        RosterEntry(
            slug="buy_and_hold_spy",
            name=f"Buy & Hold {BENCHMARK_SYMBOL}",
            kind=AgentKind.BASELINE,
            runtime=RUNTIME_BUY_AND_HOLD,
            description=(
                f"Passive full allocation to {BENCHMARK_SYMBOL}. The benchmark every "
                "active strategy has to beat, and a permanent sanity check: if its "
                "return stops tracking the index, the bug is ours."
            ),
            color="#a8a79b",
            config=_BUY_AND_HOLD_CONFIG,
        ),
        RosterEntry(
            slug="quant_sma",
            name="Quant SMA",
            kind=AgentKind.BASELINE,
            runtime=RUNTIME_QUANT_SMA,
            description=(
                "Fixed 50/200 moving-average rule, no model involved. Separates "
                "'the LLM added judgement' from 'any systematic rule would have worked'."
            ),
            color="#7a7a70",
            config=_QUANT_SMA_CONFIG,
        ),
    ]


def build_runtime(agent: Agent) -> AgentRuntime:
    """Instantiate the runtime named by `agent.runtime`.

    Cheap enough to call once per turn; runtimes hold no state between turns.
    """
    runtime = (agent.runtime or "").lower()
    config = agent.config or {}

    if runtime == RUNTIME_BUY_AND_HOLD:
        return BuyAndHoldRuntime(symbol=config.get("symbol", BENCHMARK_SYMBOL))

    if runtime == RUNTIME_QUANT_SMA:
        return QuantSmaRuntime(max_positions=int(config.get("max_positions", 5)))

    if runtime == RUNTIME_LLM:
        strategy = get_strategy(config.get("strategy", agent.slug))
        # The stored prompt wins: that is what makes re-prompting an experiment
        # rather than a deploy.
        if agent.strategy_prompt and agent.strategy_prompt != strategy.prompt:
            strategy = dataclasses.replace(strategy, prompt=agent.strategy_prompt)
        return LLMAgentRuntime(strategy)

    raise ValueError(f"unknown runtime {agent.runtime!r} for agent {agent.slug!r}")


__all__ = [
    "RUNTIME_BUY_AND_HOLD",
    "RUNTIME_LLM",
    "RUNTIME_QUANT_SMA",
    "RosterEntry",
    "build_runtime",
    "default_roster",
]
