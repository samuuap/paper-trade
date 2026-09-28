"""OpenAI-compatible inference client.

The backend only ever talks to a model through this interface. That indirection
is what lets the same trading logic run against a local `llama-server`, a
different local model, or the hosted Claude API for comparison, without a line
changing anywhere else.

Instrumentation is not an afterthought here: token counts, wall-clock inference
time, generation speed and retry counts are captured on every call, because for
this project they are as much the product as the trades are.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.pricing import breakdown

log = get_logger(__name__)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """The inference server is not reachable or not ready."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: Usage
    inference_ms: float
    finish_reason: str = "stop"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_per_second(self) -> float:
        seconds = self.inference_ms / 1000
        if seconds <= 0:
            return 0.0
        return round(self.usage.completion_tokens / seconds, 2)

    def costs(self):
        return breakdown(
            self.usage.prompt_tokens, self.usage.completion_tokens, self.inference_ms / 1000
        )


class LLMClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key
        self.timeout = timeout or settings.llm_timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=httpx.Timeout(self.timeout, connect=10.0),
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ----------------------------------------------------------------- chat
    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        grammar: Optional[str] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model or settings.llm_model_open,
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "top_p": settings.llm_top_p,
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # Grammar and tool-calling are mutually exclusive in a single request:
        # a grammar pinned to the answer schema would forbid the model from
        # emitting a tool call at all. The agent runtime therefore runs the
        # tool loop unconstrained and applies the grammar only to the final
        # answer turn.
        if grammar and not tools:
            payload["grammar"] = grammar
        elif json_schema and not tools:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "decision"),
                    "schema": json_schema,
                    "strict": True,
                },
            }

        client = await self._http()
        started = time.perf_counter()
        try:
            response = await client.post("/chat/completions", json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"inference server unreachable at {self.base_url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"inference request failed: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000

        if response.status_code >= 400:
            raise LLMError(f"inference server returned {response.status_code}: {response.text[:400]}")

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"inference server returned non-JSON: {response.text[:200]}") from exc

        return _parse_response(body, elapsed_ms)

    # --------------------------------------------------------------- health
    async def health(self) -> dict[str, Any]:
        """Liveness plus whatever the server will tell us about the model.

        `/health` and `/props` are llama.cpp specifics; a hosted endpoint
        simply will not answer them, which is reported rather than treated as
        a failure.
        """
        info: dict[str, Any] = {"base_url": self.base_url, "reachable": False}
        client = await self._http()

        root = self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url
        try:
            response = await client.get(f"{root}/health", timeout=5.0)
            info["reachable"] = response.status_code == 200
            info["status"] = response.json() if response.status_code == 200 else response.status_code
        except httpx.HTTPError as exc:
            info["error"] = str(exc)

        try:
            props = await client.get(f"{root}/props", timeout=5.0)
            if props.status_code == 200:
                data = props.json()
                info["model_path"] = data.get("model_path") or data.get("default_generation_settings", {}).get("model")
                info["context_size"] = data.get("default_generation_settings", {}).get("n_ctx")
                info["model_loaded"] = True
        except httpx.HTTPError:
            info.setdefault("model_loaded", None)

        if not info["reachable"]:
            # Fall back to the OpenAI-compatible surface, which any provider has.
            try:
                models = await client.get("/models", timeout=5.0)
                info["reachable"] = models.status_code == 200
                if models.status_code == 200:
                    info["models"] = [m.get("id") for m in models.json().get("data", [])]
            except httpx.HTTPError as exc:
                info["error"] = str(exc)
        return info


def _parse_response(body: dict[str, Any], elapsed_ms: float) -> LLMResponse:
    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"inference response contained no choices: {str(body)[:200]}")
    choice = choices[0]
    message = choice.get("message") or {}

    tool_calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                # Keep the raw text: the runtime records it as a failed call
                # rather than throwing away the evidence.
                arguments = {"__unparsed__": arguments}
        tool_calls.append(
            {
                "id": call.get("id") or f"call_{len(tool_calls)}",
                "name": function.get("name", ""),
                "arguments": arguments or {},
            }
        )

    usage_body = body.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(usage_body.get("prompt_tokens") or 0),
        completion_tokens=int(usage_body.get("completion_tokens") or 0),
    )

    return LLMResponse(
        content=(message.get("content") or "").strip(),
        model=body.get("model", ""),
        usage=usage,
        inference_ms=round(elapsed_ms, 2),
        finish_reason=choice.get("finish_reason") or "stop",
        tool_calls=tool_calls,
        raw=body,
    )


_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def set_client(client: Optional[LLMClient]) -> None:
    """Test hook."""
    global _client
    _client = client
