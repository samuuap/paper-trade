"""Tests for the instrumented LLM runtime.

Driven by `tests/fake_llm.FakeLLMClient`, which stands in for the inference
server. What is asserted here is the chain the whole design rests on:

  briefing -> tool loop -> constrained answer -> deterministic sizing -> ledger

and, just as importantly, that every step of it is measured. An agent that
trades well but records nothing is useless to this project; an agent that
records a successful turn when its model was unreachable is worse than useless.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.base import AgentContext
from app.agents.registry import build_runtime
from app.db.base import session_scope
from app.db.models import (
    Agent,
    Decision,
    DecisionKind,
    DecisionStatus,
    Order,
    Phase,
    Portfolio,
)
from app.engine.execution import ExecutionEngine
from app.llm.client import LLMUnavailable, set_client
from app.llm.pricing import shadow_cost
from tests.fake_llm import FakeLLMClient, tradable_from

pytestmark = pytest.mark.usefixtures("roster")


@pytest.fixture(autouse=True)
def restore_client():
    yield
    set_client(None)


async def run_turn(
    client,
    *,
    slug: str = "momentum",
    kind: DecisionKind = DecisionKind.TRADE,
    phase: Phase = Phase.OPEN,
) -> tuple[Decision, list]:
    set_client(client)
    async with session_scope() as session:
        agent = (await session.execute(select(Agent).where(Agent.slug == slug))).scalar_one()
        portfolio = (
            await session.execute(select(Portfolio).where(Portfolio.agent_id == agent.id))
        ).scalar_one()
        ctx = AgentContext(
            session=session,
            agent=agent,
            portfolio=portfolio,
            phase=phase,
            kind=kind,
            cycle_id=uuid.uuid4(),
            engine=ExecutionEngine(session),
        )
        decision = await build_runtime(agent).run(ctx)
        await session.flush()
        return decision, list(ctx.results)


def trade_payload(orders):
    return {
        "market_view": "Breadth improving.",
        "reasoning": "The screen lines up with the mandate.",
        "orders": orders,
    }


# --------------------------------------------------------- the happy path


def buy_first_offered(prompt: str):
    """Answer with whatever the briefing put on the table.

    Naming a ticker outright would tie the test to which symbol the screener
    happens to rank first, which is a property of the synthetic market and not
    of the code under test.
    """
    offered = tradable_from(prompt)
    if not offered:
        return trade_payload([])
    return trade_payload(
        [
            {
                "symbol": offered[0],
                "action": "BUY",
                "conviction": 0.8,
                "rationale": "Trend intact.",
            }
        ]
    )


async def test_a_valid_answer_becomes_orders_on_the_ledger():
    # `default`, not `script`: the runtime spends its first turn on the
    # unconstrained tool loop, so a one-entry script would be consumed there
    # and never reach the answer turn.
    client = FakeLLMClient(default=buy_first_offered)
    decision, results = await run_turn(client)

    assert decision is not None
    assert decision.status is DecisionStatus.OK
    assert decision.market_view == "Breadth improving."
    assert len(results) == 1
    assert results[0].filled
    # The order carries the model's own words, not a generic label.
    assert results[0].order.rationale == "Trend intact."
    assert results[0].order.conviction == pytest.approx(0.8)


async def test_an_empty_order_list_is_a_decision_not_a_failure():
    """Holding is a choice; the schema must not force a trade."""
    decision, results = await run_turn(FakeLLMClient(default=trade_payload([])))
    assert decision.status is DecisionStatus.OK
    assert results == []


async def test_every_turn_is_instrumented():
    client = FakeLLMClient(default=trade_payload([]), latency_ms=250.0)
    decision, _ = await run_turn(client)

    assert decision.prompt_tokens > 0
    assert decision.completion_tokens > 0
    assert decision.total_tokens == decision.prompt_tokens + decision.completion_tokens
    # A turn is several calls — the tool loop plus the answer — and the row
    # records the whole turn, not the last request.
    assert decision.inference_ms == pytest.approx(250.0 * len(client.calls))
    assert decision.tokens_per_second == pytest.approx(
        decision.completion_tokens / (decision.inference_ms / 1000), rel=0.01
    )
    # Running locally is not free — it is paid in a different currency.
    assert decision.energy_cost_usd > 0
    assert decision.shadow_cost_usd == pytest.approx(
        shadow_cost(decision.prompt_tokens, decision.completion_tokens)
    )
    assert decision.model == "fake-model"


# ------------------------------------------------------------- the tools


async def test_the_tool_loop_dispatches_and_feeds_results_back():
    client = FakeLLMClient(
        script=[
            {"tool": "get_quote", "arguments": {"symbol": "AAPL"}},
            trade_payload([]),
        ],
        default=trade_payload([]),
    )
    decision, _ = await run_turn(client)

    assert decision.tool_call_count == 1
    assert decision.tool_calls[0]["name"] == "get_quote"
    assert decision.tool_calls[0]["error"] is None
    # The tool result went back into the conversation before the answer turn.
    assert len(client.calls) >= 3


async def test_a_failing_tool_is_recorded_rather_than_crashing_the_turn():
    client = FakeLLMClient(
        script=[
            {"tool": "get_quote", "arguments": {"symbol": "NOT_A_TICKER"}},
            trade_payload([]),
        ],
        default=trade_payload([]),
    )
    decision, _ = await run_turn(client)

    assert decision.status is DecisionStatus.OK
    assert decision.tool_calls[0]["error"]


async def test_the_answer_turn_is_constrained_and_the_tool_turn_is_not():
    """The two cannot be combined: a grammar would forbid emitting a tool call."""
    client = FakeLLMClient(
        script=[{"tool": "get_portfolio", "arguments": {}}, trade_payload([])],
        default=trade_payload([]),
    )
    await run_turn(client)

    tool_turns = [c for c in client.calls if c["tools"]]
    answer_turns = [c for c in client.calls if not c["tools"]]
    assert tool_turns and all(not c["grammar"] for c in tool_turns)
    assert answer_turns and all(c["grammar"] for c in answer_turns)


# ----------------------------------------------------------- the failures


async def test_invalid_output_is_retried_and_counted():
    """The claim the grammar exists to make unnecessary, measured anyway."""
    client = FakeLLMClient(
        # First entry ends the tool loop; the next two are the answer turn
        # failing, and the last one is it finally conforming.
        script=["ready to answer", "not json at all", "still not json", trade_payload([])],
        default=trade_payload([]),
    )
    decision, _ = await run_turn(client)

    assert decision.status is DecisionStatus.OK
    assert decision.retries == 2


async def test_persistently_invalid_output_is_recorded_as_such():
    client = FakeLLMClient(default="{ this is never going to parse")
    decision, results = await run_turn(client)

    assert decision.status is DecisionStatus.INVALID_OUTPUT
    assert decision.error
    assert results == []


async def test_a_semantically_invalid_answer_is_rejected_by_pydantic():
    """The grammar cannot express 0 <= conviction <= 1; validation must."""
    client = FakeLLMClient(
        default=trade_payload(
            [{"symbol": "AAPL", "action": "BUY", "conviction": 7.0, "rationale": "x"}]
        )
    )
    decision, results = await run_turn(client)

    assert decision.status is DecisionStatus.INVALID_OUTPUT
    assert results == []


async def test_an_unreachable_model_is_an_error_not_a_hold():
    """Silence and 'I chose to do nothing' must never look the same."""

    class Unreachable(FakeLLMClient):
        async def chat(self, *args, **kwargs):
            raise LLMUnavailable("inference server unreachable")

    decision, results = await run_turn(Unreachable())
    assert decision.status is DecisionStatus.ERROR
    assert "unreachable" in decision.error
    assert results == []


async def test_a_ticker_outside_the_briefing_cannot_reach_the_ledger():
    """Belt and braces for `structured_mode=none`: the grammar is off here."""
    client = FakeLLMClient(
        default=trade_payload(
            [{"symbol": "AAPL", "action": "BUY", "conviction": 0.5, "rationale": "ok"},
             {"symbol": "TSLA", "action": "BUY", "conviction": 0.5, "rationale": "not offered"}]
        )
    )
    decision, results = await run_turn(client)

    async with session_scope() as session:
        orders = (
            await session.execute(select(Order).where(Order.decision_id == decision.id))
        ).scalars().all()
    assert {o.symbol for o in orders} <= {"AAPL"}


# -------------------------------------------------------- the other phases


@pytest.mark.parametrize(
    ("kind", "phase", "payload"),
    [
        (
            DecisionKind.PLAN,
            Phase.PRE_MARKET,
            {"market_view": "Gaps up.", "reasoning": "r", "watchlist": ["AAPL"], "plan": "Wait."},
        ),
        (
            DecisionKind.REVIEW,
            Phase.AFTER_HOURS,
            {"summary": "Flat day.", "attribution": "a", "lessons": "l", "journal": "j"},
        ),
        (
            DecisionKind.RESEARCH,
            Phase.CLOSED,
            {"summary": "Regime unclear.", "hypotheses": ["h1"], "strategy_notes": "n"},
        ),
    ],
)
async def test_non_trading_phases_produce_narrative_and_never_orders(kind, phase, payload):
    decision, results = await run_turn(
        FakeLLMClient(default=payload), kind=kind, phase=phase
    )
    assert decision.status is DecisionStatus.OK
    assert decision.market_view or decision.reasoning
    assert results == []


async def test_the_raw_response_is_kept_for_auditing():
    payload = trade_payload([])
    decision, _ = await run_turn(FakeLLMClient(default=payload))
    assert json.loads(decision.raw_response) == payload
