"""In-process publish/subscribe bus backing the dashboard's SSE stream.

Deliberately tiny: the orchestrator, engine and ingest layer publish events, the
API fans them out to connected browsers. Subscribers that fall behind get their
oldest events dropped rather than stalling the publisher — a slow dashboard tab
must never be able to back-pressure the trading loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

MAX_QUEUE = 256
REPLAY_BUFFER = 100


@dataclass
class Event:
    topic: str
    data: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_payload(self) -> dict[str, Any]:
        return {"topic": self.topic, "ts": self.ts.isoformat(), "data": self.data}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._recent: deque[Event] = deque(maxlen=REPLAY_BUFFER)
        self._lock = asyncio.Lock()

    def publish(self, topic: str, **data: Any) -> None:
        """Fire-and-forget. Safe to call from anywhere on the loop."""
        event = Event(topic=topic, data=data)
        self._recent.append(event)
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def recent(self, topic: Optional[str] = None) -> list[Event]:
        if topic is None:
            return list(self._recent)
        return [e for e in self._recent if e.topic == topic]

    async def subscribe(self, topics: Optional[set[str]] = None) -> AsyncIterator[Event]:
        """Async-iterator view of the stream.

        Convenient, but see `listen` before reaching for it in code that needs
        a timeout.
        """
        async with self.listen() as queue:
            while True:
                event = await queue.get()
                if topics is None or event.topic in topics:
                    yield event

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[asyncio.Queue]:
        """Raw queue subscription.

        Exists because a consumer that needs a timeout — the SSE route, which
        emits a keepalive during quiet stretches — cannot use `subscribe`:
        cancelling an async generator's `__anext__()` leaves the generator
        permanently broken, so the *first* keepalive would kill the stream.
        Cancelling `queue.get()` is safe, so the timeout goes here instead.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def clear(self) -> None:
        """Drop the replay buffer.

        Used between tests, where a leftover buffer makes assertions depend on
        which test ran before, and available for a clean restart.
        """
        self._recent.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


bus = EventBus()
