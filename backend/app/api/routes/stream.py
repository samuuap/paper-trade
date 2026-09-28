"""Server-sent events: the dashboard's live feed.

SSE rather than websockets because the traffic is one-directional and SSE
reconnects on its own. The bus behind it drops the oldest events for a
subscriber that falls behind, so a browser tab left open on a laptop that went
to sleep can never apply back-pressure to the trading loop.

The generator is a plain function rather than a closure inside the route, so
the interesting behaviour — replay on connect, topic filtering, the keepalive —
can be tested without an HTTP transport in the way.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import AsyncIterator, Awaitable, Callable, Optional

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.core.config import settings
from app.core.events import bus

router = APIRouter(tags=["stream"])

#: Topics published across the system. Listed so a client can subscribe
#: selectively instead of filtering a firehose.
TOPICS = ("phase", "cycle", "decision", "order", "fill", "equity", "queue", "job", "system")

DisconnectCheck = Callable[[], Awaitable[bool]]


def _encode(payload: dict) -> str:
    return json.dumps(payload, default=str)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def event_publisher(
    *,
    topics: Optional[set[str]] = None,
    replay: int = 20,
    is_disconnected: Optional[DisconnectCheck] = None,
    keepalive_seconds: Optional[float] = None,
) -> AsyncIterator[dict]:
    """Yield SSE frames until the client goes away.

    The replay buffer goes out first so a page load does not have to wait for
    the next event to show anything — which, between 15-minute cycles, could be
    a long and confusing wait.
    """
    timeout = keepalive_seconds or settings.sse_keepalive_seconds

    if replay:
        for event in bus.recent()[-replay:]:
            if topics is None or event.topic in topics:
                yield {"event": event.topic, "data": _encode(event.to_payload())}

    # `bus.listen` rather than `bus.subscribe`: the keepalive needs a timeout,
    # and cancelling an async generator's `__anext__()` breaks it permanently.
    # Cancelling a queue read does not. See EventBus.listen.
    async with bus.listen() as queue:
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                # Keeps proxies from timing the idle connection out during the
                # quiet stretches, which here are most of the day.
                yield {"event": "keepalive", "data": _encode({"ts": _now()})}
                continue
            if topics is not None and event.topic not in topics:
                continue
            yield {"event": event.topic, "data": _encode(event.to_payload())}


@router.get("/stream")
async def stream(
    request: Request,
    topics: Optional[str] = Query(None, description=f"Comma-separated. Known: {', '.join(TOPICS)}"),
    replay: int = Query(20, ge=0, le=100, description="Recent events to send on connect"),
) -> EventSourceResponse:
    """Live event stream."""
    wanted = {t.strip() for t in topics.split(",") if t.strip()} if topics else None
    return EventSourceResponse(
        event_publisher(
            topics=wanted, replay=replay, is_disconnected=request.is_disconnected
        )
    )


@router.get("/stream/topics")
async def list_topics() -> dict[str, list[str]]:
    return {"topics": list(TOPICS)}
