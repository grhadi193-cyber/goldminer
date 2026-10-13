"""Token-bucket rate limiter + backoff helpers (R9: be polite to free sources)."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass


class RateLimiter:
    """Async token bucket. `rate_per_min` tokens refill continuously."""

    def __init__(self, rate_per_min: float, burst: float | None = None) -> None:
        self.rate_per_sec = max(rate_per_min, 0.0001) / 60.0
        self.capacity = burst if burst is not None else max(1.0, rate_per_min / 6.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_sec)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self.rate_per_sec)


@dataclass
class Backoff:
    """Exponential backoff with full jitter. Used for 429/5xx/overload ONLY."""

    base: float = 1.0
    factor: float = 2.0
    max_sleep: float = 60.0

    def delay(self, attempt: int) -> float:
        raw = min(self.max_sleep, self.base * (self.factor ** max(0, attempt - 1)))
        return random.uniform(0.0, raw)

    async def sleep(self, attempt: int) -> None:
        await asyncio.sleep(self.delay(attempt))
