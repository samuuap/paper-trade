"""Sequential inference queue.

All agents share a single model resident in unified memory, so their decisions
are queued rather than run in parallel. With a 15-minute cadence there is ample
headroom, and serialising avoids several copies of a model fighting over the
same RAM — which on a 36 GB laptop is the difference between working and
swapping.

The queue's depth and wait times are first-class metrics: if the queue starts
backing up, decisions are arriving faster than the hardware can answer them,
and that is something the dashboard should show before it becomes a mystery.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar

from app.core.events import bus
from app.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass
class QueueStats:
    depth: int = 0
    running: Optional[str] = None
    completed: int = 0
    failed: int = 0
    max_wait_ms: float = 0.0
    avg_wait_ms: float = 0.0
    total_wait_ms: float = 0.0
    total_run_ms: float = 0.0
    waiting: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "running": self.running,
            "waiting": self.waiting,
            "completed": self.completed,
            "failed": self.failed,
            "max_wait_ms": round(self.max_wait_ms, 1),
            "avg_wait_ms": round(self.avg_wait_ms, 1),
            "total_run_seconds": round(self.total_run_ms / 1000, 1),
        }


@dataclass
class QueuedResult:
    value: Any
    wait_ms: float
    run_ms: float


class InferenceQueue:
    """FIFO, single-slot by default. `asyncio.Lock` wakes waiters in order."""

    def __init__(self, concurrency: int = 1) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._waiting: list[str] = []
        self._running: Optional[str] = None
        self._completed = 0
        self._failed = 0
        self._total_wait_ms = 0.0
        self._total_run_ms = 0.0
        self._max_wait_ms = 0.0

    @property
    def depth(self) -> int:
        return len(self._waiting) + (1 if self._running else 0)

    async def run(self, label: str, fn: Callable[[], Awaitable[T]]) -> QueuedResult:
        self._waiting.append(label)
        bus.publish("queue", depth=self.depth, waiting=list(self._waiting), running=self._running)
        queued_at = time.perf_counter()
        try:
            await self._semaphore.acquire()
        finally:
            # Whether we got the slot or were cancelled, we are no longer waiting.
            if label in self._waiting:
                self._waiting.remove(label)

        wait_ms = (time.perf_counter() - queued_at) * 1000
        self._total_wait_ms += wait_ms
        self._max_wait_ms = max(self._max_wait_ms, wait_ms)
        self._running = label
        bus.publish("queue", depth=self.depth, waiting=list(self._waiting), running=label)

        started = time.perf_counter()
        try:
            value = await fn()
        except Exception:
            self._failed += 1
            raise
        else:
            self._completed += 1
            return QueuedResult(
                value=value, wait_ms=round(wait_ms, 2), run_ms=round((time.perf_counter() - started) * 1000, 2)
            )
        finally:
            run_ms = (time.perf_counter() - started) * 1000
            self._total_run_ms += run_ms
            self._running = None
            self._semaphore.release()
            bus.publish("queue", depth=self.depth, waiting=list(self._waiting), running=None)

    def stats(self) -> QueueStats:
        total_jobs = self._completed + self._failed
        return QueueStats(
            depth=self.depth,
            running=self._running,
            completed=self._completed,
            failed=self._failed,
            max_wait_ms=self._max_wait_ms,
            avg_wait_ms=self._total_wait_ms / total_jobs if total_jobs else 0.0,
            total_wait_ms=self._total_wait_ms,
            total_run_ms=self._total_run_ms,
            waiting=list(self._waiting),
        )


inference_queue = InferenceQueue(concurrency=1)
