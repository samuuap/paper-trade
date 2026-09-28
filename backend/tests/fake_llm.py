"""A scripted inference server, in-process.

The LLM runtime is the largest module in the system and the one that is
hardest to exercise, because the obvious way to test it needs a model. This
stands in for one: it speaks the same `LLMClient.chat` interface, returns
deterministic answers, and reports plausible token counts and latency so the
instrumentation path is exercised rather than bypassed.

What it makes testable, and nothing else can:

* the tool loop — that a tool call is dispatched, its result fed back, and the
  conversation continued;
* the constrained answer turn — that a valid payload is parsed, validated and
  turned into orders;
* the retry path — that invalid output is retried and counted, which is the
  behaviour the whole grammar argument rests on;
* the instrumentation — tokens, latency, tokens/second, retries and shadow
  cost all landing on the `decisions` row.

It is a test double, so it lives in `tests/`. The real client's contract is in
`app/llm/client.py`; if that changes, this must follow.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any, Callable, Optional

from app.llm.client import LLMResponse, Usage

#: Roughly four characters per token. Close enough that the recorded figures
#: have the right order of magnitude, which is all a test needs.
CHARS_PER_TOKEN = 4


def _estimate(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


class FakeLLMClient:
    """Replays a scripted sequence of answers.

    `script` is consumed in order; once exhausted, `default` is returned for
    every further call. Each entry is either a JSON-serialisable payload (the
    final answer), a raw string (to exercise the invalid-output path), or a
    tool call spec: `{"tool": name, "arguments": {...}}`.
    """

    def __init__(
        self,
        script: Optional[list[Any]] = None,
        default: Optional[Any] = None,
        model: str = "fake-model",
        latency_ms: float = 120.0,
    ) -> None:
        self.script = list(script or [])
        self.default = default if default is not None else {}
        self.model = model
        self.latency_ms = latency_ms
        self.calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"base_url": "fake://", "reachable": True, "model_loaded": True}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        grammar: Optional[str] = None,
        json_schema: Optional[dict[str, Any]] = None,
        **_: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": len(messages),
                "tools": [t["function"]["name"] for t in tools or []],
                "grammar": grammar is not None,
                "json_schema": json_schema is not None,
                "model": model,
            }
        )

        item = self.script.pop(0) if self.script else self.default

        # A callable entry is handed the whole prompt, so a test can answer
        # with a symbol the briefing actually offered instead of guessing one.
        if callable(item):
            item = item("\n".join(str(m.get("content", "")) for m in messages))

        if isinstance(item, dict) and "tool" in item:
            # A tool call is only expressible on an unconstrained turn; if the
            # runtime asked for one here it would be a bug worth failing on.
            assert tools, "the fake was scripted to call a tool on a turn with no tools"
            return self._response(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{len(self.calls)}",
                        "name": item["tool"],
                        "arguments": item.get("arguments", {}),
                    }
                ],
                prompt_chars=sum(len(str(m.get("content", ""))) for m in messages),
            )

        content = item if isinstance(item, str) else json.dumps(item)
        return self._response(
            content=content,
            tool_calls=[],
            prompt_chars=sum(len(str(m.get("content", ""))) for m in messages),
        )

    def _response(
        self, *, content: str, tool_calls: list[dict[str, Any]], prompt_chars: int
    ) -> LLMResponse:
        usage = Usage(
            prompt_tokens=_estimate("x" * prompt_chars),
            completion_tokens=_estimate(content) if content else 12,
        )
        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            inference_ms=self.latency_ms,
            tool_calls=tool_calls,
        )


class StrategyLLMClient:
    """Answers from the briefing instead of from a script.

    Used to generate a demo run: it reads the candidate table out of the
    prompt and picks from it, so the decisions look like something an agent
    would plausibly produce rather than the same ticker every cycle. Seeded,
    so two runs of the same window produce the same history.
    """

    def __init__(
        self,
        seed: str = "demo",
        model: str = "fake-model",
        pick: int = 2,
        chooser: Optional[Callable[[list[str], random.Random], list[str]]] = None,
    ) -> None:
        self.seed = seed
        self.model = model
        self.pick = pick
        self.chooser = chooser
        self.turn = 0

    async def aclose(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"base_url": "fake://", "reachable": True, "model_loaded": True}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        grammar: Optional[str] = None,
        **_: Any,
    ) -> LLMResponse:
        self.turn += 1
        prompt = "\n".join(str(m.get("content", "")) for m in messages)

        # Skip the tool loop entirely: this client exists to produce history,
        # not to exercise tool dispatch (FakeLLMClient covers that).
        if tools:
            return self._response("Ready to answer.", prompt)

        allowed = tradable_from(prompt)
        # Seeded per agent as well as per turn, so two strategies looking at
        # the same candidate list do not make identical choices — otherwise
        # every curve in a demo run is the same curve.
        rng = random.Random(f"{self.seed}:{_agent_key(prompt)}:{self.turn}")
        if not allowed:
            payload = {"market_view": "Nothing screenable.", "reasoning": "Holding.", "orders": []}
            return self._response(json.dumps(payload), prompt)

        chosen = (
            self.chooser(allowed, rng)
            if self.chooser
            else rng.sample(allowed, k=min(self.pick, len(allowed)))
        )
        orders = [
            {
                "symbol": symbol,
                "action": "BUY" if rng.random() > 0.3 else "SELL",
                "conviction": round(rng.uniform(0.3, 0.95), 2),
                "rationale": "Signals line up with the strategy.",
            }
            for symbol in chosen
        ]
        payload = {
            "market_view": "Breadth is mixed; sticking to the screen.",
            "reasoning": "Acting on the pre-computed candidates that fit the mandate.",
            "orders": orders,
        }
        return self._response(json.dumps(payload), prompt)

    def _response(self, content: str, prompt: str) -> LLMResponse:
        return LLMResponse(
            content=content,
            model=self.model,
            usage=Usage(prompt_tokens=_estimate(prompt), completion_tokens=_estimate(content)),
            inference_ms=random.Random(f"{self.seed}:{self.turn}:ms").uniform(800, 4200),
            tool_calls=[],
        )


def _agent_key(prompt: str) -> str:
    """Identify the agent from its system prompt."""
    match = re.search(r"YOUR STRATEGY: (.+?) ---", prompt)
    return match.group(1).strip() if match else "unknown"


def tradable_from(prompt: str) -> list[str]:
    """Recover the symbol whitelist the briefing handed the model."""
    match = re.search(r"You may only act on these symbols: (.+)", prompt)
    if not match:
        return []
    return [s.strip() for s in match.group(1).split(",") if s.strip()]
