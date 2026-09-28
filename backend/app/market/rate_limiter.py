"""Token-bucket rate limiter for the market-data provider.

Finnhub's free tier allows 60 requests/minute. Going over gets you throttled at
exactly the moment you least want it, so the ingest layer asks this limiter for
permission before every call and the limiter's own consumption is published as
a dashboard metric.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimiterStats:
    limit_per_minute: int
    used_last_minute: int
    remaining: int
    total_requests: int = 0
    total_throttled: int = 0
    total_wait_seconds: float = 0.0
    utilisation: float = field(default=0.0)


class RateLimiter:
    """Sliding-window limiter. Fair (FIFO) because it holds a single lock."""

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit_per_minute)
        self.window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._total_throttled = 0
        self._total_wait = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    async def acquire(self, weight: int = 1) -> float:
        """Block until `weight` slots are free. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._timestamps) + weight <= self.limit:
                    for _ in range(weight):
                        self._timestamps.append(now)
                    self._total_requests += weight
                    self._total_wait += waited
                    return waited
                sleep_for = self._timestamps[0] + self.window - now
                self._total_throttled += 1
            sleep_for = max(sleep_for, 0.01)
            waited += sleep_for
            await asyncio.sleep(sleep_for)

    def stats(self) -> RateLimiterStats:
        now = time.monotonic()
        self._prune(now)
        used = len(self._timestamps)
        return RateLimiterStats(
            limit_per_minute=self.limit,
            used_last_minute=used,
            remaining=max(0, self.limit - used),
            total_requests=self._total_requests,
            total_throttled=self._total_throttled,
            total_wait_seconds=round(self._total_wait, 2),
            utilisation=round(used / self.limit, 4) if self.limit else 0.0,
        )

    def estimated_seconds_for(self, requests: int) -> float:
        """How long `requests` calls will take at this budget. Used for planning."""
        if requests <= self.limit:
            return 0.0
        return (requests - self.limit) / self.limit * self.window
