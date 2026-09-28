"""The instrumented LLM agent runtime.

Shape of one turn:

1. **Briefing** — Python assembles the portfolio state, a pre-computed
   shortlist from the agent's screener and the benchmark context. The model
   starts with real material rather than an empty prompt.
2. **Tool loop** — the model may call read-only tools to dig into specific
   names. Unconstrained, because a grammar pinned to the answer schema would
   make emitting a tool call impossible.
3. **Answer** — one final turn under a GBNF grammar whose `symbol` field is an
   enum of exactly the tickers in play. Malformed output is impossible and an
   invented ticker is impossible.
4. **Execution** — proposals go through deterministic sizing and risk checks
   before reaching the ledger.

Every step is measured. Tokens, latency, generation speed, tool calls, retries,
shadow cost and energy cost all land on the `decisions` row, because for this
project the instrumentation *is* the deliverable.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.agents import tools as tool_module
from app.agents.base import AgentContext, new_decision
from app.agents.strategies import Strategy, get_strategy
from app.core.config import settings
from app.core.events import bus
from app.core.logging import get_logger
from app.db.models import DecisionKind, DecisionStatus, OrderSide, OrderType
from app.engine.risk import OrderIntent
from app.llm import schemas
from app.llm.client import LLMError, LLMResponse, LLMUnavailable, Usage, get_client
from app.llm.grammar import json_schema_to_gbnf
from app.llm.queue import inference_queue

log = get_logger(__name__)

#: How many pre-screened candidates the briefing carries. Enough to choose
#: from, small enough to stay cheap on a local model.
BRIEFING_CANDIDATES = 15

TASK_PROMPTS = {
    DecisionKind.TRADE: (
        "It is a trading cycle. Review your portfolio and the candidates below, then "
        "decide what to do right now. You may call tools to investigate specific names "
        "before answering. Propose at most {max_orders} orders; an empty list means hold."
    ),
    DecisionKind.PLAN: (
        "The market has not opened yet. Digest the overnight picture, set a watchlist "
        "and state your plan for the session. You are not placing trades now."
    ),
    DecisionKind.REVIEW: (
        "The session has closed. Review how your portfolio did, attribute the P&L to "
        "specific positions and record what you learned. You are not placing trades now."
    ),
    DecisionKind.RESEARCH: (
        "The market is closed. Use this quiet time for deeper research: study your "
        "holdings and the broader market, and reflect on whether your strategy is "
        "working. You are not placing trades now."
    ),
}


class LLMAgentRuntime:
    """One instance per strategy; stateless between turns."""

    def __init__(self, strategy: Strategy) -> None:
        self.strategy = strategy
        self.name = strategy.slug

    # ------------------------------------------------------------------ run
    async def run(self, ctx: AgentContext):
        model = _model_for(ctx)
        decision = new_decision(ctx, model=model)

        try:
            briefing = await self._build_briefing(ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("agent.briefing_failed", agent=ctx.agent.slug)
            decision.status = DecisionStatus.ERROR
            decision.error = f"briefing failed: {exc}"
            ctx.session.add(decision)
            return decision

        messages = [
            {"role": "system", "content": self.strategy.system_prompt()},
            {"role": "user", "content": briefing["prompt"]},
        ]

        label = f"{ctx.agent.slug}:{ctx.kind.value.lower()}"
        try:
            queued = await inference_queue.run(
                label, lambda: self._converse(ctx, messages, briefing, model)
            )
        except LLMUnavailable as exc:
            log.warning("agent.llm_unavailable", agent=ctx.agent.slug, error=str(exc))
            decision.status = DecisionStatus.ERROR
            decision.error = str(exc)
            ctx.session.add(decision)
            bus.publish("decision", agent=ctx.agent.slug, status="ERROR", error=str(exc))
            return decision
        except LLMError as exc:
            log.warning("agent.llm_error", agent=ctx.agent.slug, error=str(exc))
            decision.status = DecisionStatus.ERROR
            decision.error = str(exc)
            ctx.session.add(decision)
            return decision

        outcome: _Outcome = queued.value
        decision.queue_wait_ms = queued.wait_ms
        _apply_instrumentation(decision, outcome)

        if outcome.payload is None:
            decision.status = DecisionStatus.INVALID_OUTPUT
            decision.error = outcome.error or "model did not produce a valid decision"
            ctx.session.add(decision)
            await ctx.session.flush()
            bus.publish(
                "decision",
                agent=ctx.agent.slug,
                agent_id=str(ctx.agent.id),
                status=decision.status.value,
                kind=ctx.kind.value,
            )
            return decision

        decision.output = outcome.payload
        decision.status = DecisionStatus.OK
        _apply_narrative(decision, ctx.kind, outcome.payload)

        ctx.session.add(decision)
        await ctx.session.flush()

        if ctx.kind is DecisionKind.TRADE:
            await self._execute(ctx, decision, outcome.payload, briefing["tradable"])

        log.info(
            "agent.decided",
            agent=ctx.agent.slug,
            kind=ctx.kind.value,
            orders=len(decision.output.get("orders", []) or []),
            tokens=decision.total_tokens,
            inference_ms=round(decision.inference_ms),
            tok_per_s=decision.tokens_per_second,
            retries=decision.retries,
        )
        bus.publish(
            "decision",
            agent=ctx.agent.slug,
            agent_id=str(ctx.agent.id),
            status=decision.status.value,
            kind=ctx.kind.value,
            phase=ctx.phase.value,
            reasoning=decision.reasoning[:400],
            orders=len(decision.output.get("orders", []) or []),
            tokens=decision.total_tokens,
            inference_ms=decision.inference_ms,
            tokens_per_second=decision.tokens_per_second,
            shadow_cost_usd=decision.shadow_cost_usd,
        )
        return decision

    # ------------------------------------------------------------- briefing
    async def _build_briefing(self, ctx: AgentContext) -> dict[str, Any]:
        portfolio = await tool_module.get_portfolio(ctx)
        market = await tool_module.get_market_context(ctx)
        held = [row["symbol"] for row in portfolio["positions"]]

        screen = await tool_module.screen_stocks(
            ctx,
            preset=self.strategy.default_preset,
            limit=BRIEFING_CANDIDATES,
        )
        candidates = screen["candidates"]
        candidate_symbols = [c["symbol"] for c in candidates]

        # The answer grammar is pinned to this set: candidates it can buy plus
        # positions it can sell. Nothing else is expressible.
        tradable = sorted(dict.fromkeys(candidate_symbols + held))

        sections = [
            f"Phase: {ctx.phase.value}  |  Time: {ctx.now.isoformat(timespec='minutes')}",
            "",
            "YOUR PORTFOLIO",
            _format_portfolio(portfolio),
            "",
            "MARKET CONTEXT",
            _format_market(market),
            "",
            f"CANDIDATES (your '{screen['preset']}' screen, signals pre-computed)",
            _format_candidates(candidates),
        ]

        note = self.strategy.note_for(ctx.kind)
        task = TASK_PROMPTS[ctx.kind].format(max_orders=settings.max_orders_per_cycle)
        sections += ["", "TASK", task]
        if note:
            sections += [note]
        if tradable:
            sections += [
                "",
                "You may only act on these symbols: " + ", ".join(tradable),
            ]

        return {
            "prompt": "\n".join(sections),
            "tradable": tradable,
            "candidates": candidates,
            "held": held,
        }

    # ----------------------------------------------------------- conversation
    async def _converse(
        self,
        ctx: AgentContext,
        messages: list[dict[str, Any]],
        briefing: dict[str, Any],
        model: str,
    ) -> "_Outcome":
        client = get_client()
        outcome = _Outcome()
        tool_specs = tool_module.openai_tool_specs(self.strategy.tools)

        # --- phase 1: tool loop, unconstrained
        for iteration in range(settings.llm_max_tool_iterations):
            if not tool_specs:
                break
            response = await client.chat(messages, model=model, tools=tool_specs)
            outcome.absorb(response)
            if not response.tool_calls:
                # The model is ready to answer; keep its text as a scratchpad.
                if response.content:
                    outcome.scratchpad = response.content
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"]),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                started = time.perf_counter()
                result = await tool_module.dispatch(ctx, call["name"], call["arguments"])
                outcome.tool_calls.append(
                    {
                        "iteration": iteration,
                        "name": call["name"],
                        "arguments": call["arguments"],
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "error": result.get("error") if isinstance(result, dict) else None,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": json.dumps(result, default=str)[:6000],
                    }
                )

        # --- phase 2: the constrained answer
        schema = schemas.schema_for(
            ctx.kind,
            symbols=briefing["tradable"] or None,
            max_orders=settings.max_orders_per_cycle,
        )
        grammar = None
        json_schema = None
        if settings.structured_mode == "grammar":
            grammar = json_schema_to_gbnf(schema)
        elif settings.structured_mode == "json_schema":
            json_schema = schema

        messages.append(
            {
                "role": "user",
                "content": (
                    "Now give your final answer as a single JSON object matching the "
                    "required schema. No prose, no markdown fence."
                ),
            }
        )

        for attempt in range(settings.llm_max_retries):
            response = await client.chat(
                messages, model=model, grammar=grammar, json_schema=json_schema
            )
            outcome.absorb(response)
            outcome.raw_response = response.content

            payload, error = _parse_and_validate(ctx.kind, response.content)
            if payload is not None:
                outcome.payload = payload
                return outcome

            outcome.error = error
            outcome.retries = attempt + 1
            log.warning(
                "agent.invalid_output",
                agent=ctx.agent.slug,
                attempt=attempt + 1,
                error=error,
                mode=settings.structured_mode,
            )
            # Show the model its own broken output; without the grammar this is
            # the only lever, and with it this branch should never be reached.
            messages.append({"role": "assistant", "content": response.content[:1500]})
            messages.append(
                {
                    "role": "user",
                    "content": f"That was not valid: {error}. Reply with the JSON object only.",
                }
            )

        return outcome

    # ------------------------------------------------------------- execution
    async def _execute(
        self, ctx: AgentContext, decision, payload: dict[str, Any], tradable: list[str]
    ) -> None:
        proposals = payload.get("orders") or []
        if not proposals:
            return

        allowed = set(tradable)
        intents: list[OrderIntent] = []
        for proposal in proposals[: settings.max_orders_per_cycle]:
            symbol = str(proposal.get("symbol", "")).upper()
            if symbol not in allowed:
                # Only reachable when the grammar is off; recorded rather than
                # silently dropped so the mode comparison stays honest.
                log.warning("agent.symbol_out_of_scope", agent=ctx.agent.slug, symbol=symbol)
                continue
            action = str(proposal.get("action", "")).upper()
            if action not in ("BUY", "SELL"):
                continue
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
                    conviction=float(proposal.get("conviction", 0.5) or 0.5),
                    order_type=OrderType.MARKET,
                    rationale=str(proposal.get("rationale", ""))[:1000],
                )
            )

        if not intents:
            return

        context = await ctx.risk_context([i.symbol for i in intents])
        results = await ctx.engine.submit_many(
            ctx.agent, ctx.portfolio, intents, context, decision.id
        )
        ctx.results.extend(results)
        ctx.invalidate_risk_context()


# ------------------------------------------------------------------ helpers


class _Outcome:
    """Accumulates everything measurable across a multi-call turn."""

    def __init__(self) -> None:
        self.usage = Usage()
        self.inference_ms = 0.0
        self.calls = 0
        self.tool_calls: list[dict[str, Any]] = []
        self.retries = 0
        self.payload: Optional[dict[str, Any]] = None
        self.raw_response = ""
        self.scratchpad = ""
        self.error: Optional[str] = None
        self.model = ""

    def absorb(self, response: LLMResponse) -> None:
        self.usage = self.usage + response.usage
        self.inference_ms += response.inference_ms
        self.calls += 1
        self.model = response.model or self.model


def _apply_instrumentation(decision, outcome: _Outcome) -> None:
    seconds = outcome.inference_ms / 1000
    decision.prompt_tokens = outcome.usage.prompt_tokens
    decision.completion_tokens = outcome.usage.completion_tokens
    decision.total_tokens = outcome.usage.total_tokens
    decision.inference_ms = round(outcome.inference_ms, 2)
    decision.tokens_per_second = (
        round(outcome.usage.completion_tokens / seconds, 2) if seconds > 0 else 0.0
    )
    decision.tool_call_count = len(outcome.tool_calls)
    decision.tool_calls = outcome.tool_calls
    decision.retries = outcome.retries
    decision.raw_response = (outcome.raw_response or "")[:20000]
    if outcome.model:
        decision.model = outcome.model

    from app.llm.pricing import breakdown

    costs = breakdown(
        outcome.usage.prompt_tokens, outcome.usage.completion_tokens, seconds
    )
    decision.shadow_cost_usd = costs.shadow_usd
    decision.energy_cost_usd = costs.energy_usd


def _apply_narrative(decision, kind: DecisionKind, payload: dict[str, Any]) -> None:
    if kind is DecisionKind.REVIEW:
        decision.market_view = payload.get("summary", "")[:4000]
        decision.reasoning = "\n\n".join(
            filter(None, [payload.get("attribution", ""), payload.get("lessons", ""), payload.get("journal", "")])
        )[:8000]
        return
    if kind is DecisionKind.RESEARCH:
        decision.market_view = payload.get("summary", "")[:4000]
        hypotheses = payload.get("hypotheses") or []
        decision.reasoning = "\n".join(
            [f"- {h}" for h in hypotheses] + [payload.get("strategy_notes", "")]
        )[:8000]
        return
    decision.market_view = payload.get("market_view", "")[:4000]
    decision.reasoning = payload.get("reasoning", "")[:8000]
    if kind is DecisionKind.PLAN and payload.get("plan"):
        decision.reasoning = f"{decision.reasoning}\n\nPlan: {payload['plan']}"[:8000]


def _parse_and_validate(kind: DecisionKind, content: str):
    """Parse the answer and enforce the constraints a grammar cannot express."""
    text = (content or "").strip()
    if not text:
        return None, "empty response"

    # Belt and braces for the `structured_mode=none` comparison runs.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None, "no JSON object found in response"
        text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg} at position {exc.pos}"
    if not isinstance(payload, dict):
        return None, "top-level value is not an object"

    try:
        validated = schemas.validate_output(kind, payload)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        return None, f"schema validation failed: {str(exc)[:300]}"
    return validated.model_dump(), None


def _model_for(ctx: AgentContext) -> str:
    from app.orchestrator.resources import model_for_phase

    return ctx.agent.model or model_for_phase(ctx.phase)


def _format_portfolio(data: dict[str, Any]) -> str:
    lines = [
        f"  Equity ${data['equity']:,.2f} | Cash ${data['cash']:,.2f} | "
        f"Return {data['total_return_pct']:+.2f}% | Positions {data['position_count']}"
    ]
    if not data["positions"]:
        lines.append("  (no open positions)")
    for row in data["positions"]:
        lines.append(
            f"  {row['symbol']:<6} {row['quantity']:>10,.2f} sh @ ${row['avg_entry_price']:,.2f} "
            f"-> ${row['price']:,.2f}  P&L {row['unrealized_pnl_pct']:+.2f}%  "
            f"weight {row['weight_pct']:.1f}%"
        )
    return "\n".join(lines)


def _format_market(data: dict[str, Any]) -> str:
    def fmt(value, suffix="", digits=2):
        return f"{value:.{digits}f}{suffix}" if isinstance(value, (int, float)) else "n/a"

    return (
        f"  {data['benchmark']} {fmt(data.get('price'))} "
        f"({fmt(data.get('change_pct'), '%')} today, {fmt(data.get('return_21d_pct'), '%')} over 21d) | "
        f"above 50d: {data.get('above_sma_50')} | above 200d: {data.get('above_sma_200')} | "
        f"vol20 {fmt(data.get('volatility_20d'), '', 3)}"
    )


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "  (screen returned nothing — daily bars may not be ingested yet)"
    header = (
        f"  {'SYM':<6}{'PRICE':>9}{'CHG%':>7}{'21D%':>8}{'63D%':>8}{'RSI':>6}"
        f"{'vsSMA50':>9}{'52wHI%':>8}{'VOL×':>6}  SCORE"
    )
    def num(row, key, digits=2, default="   n/a"):
        value = row.get(key)
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else default

    lines = [header]
    for c in candidates:
        lines.append(
            f"  {c['symbol']:<6}{num(c, 'price'):>9}{num(c, 'change_pct'):>7}"
            f"{num(c, 'return_21d_pct'):>8}{num(c, 'return_63d_pct'):>8}{num(c, 'rsi_14', 1):>6}"
            f"{num(c, 'dist_sma_50_pct', 1):>9}{num(c, 'pct_from_52w_high', 1):>8}"
            f"{num(c, 'volume_ratio', 1):>6}  {c.get('score', 0):.2f}"
        )
    return "\n".join(lines)


def build_runtime(slug: str) -> LLMAgentRuntime:
    return LLMAgentRuntime(get_strategy(slug))
