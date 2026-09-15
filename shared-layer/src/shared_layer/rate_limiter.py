"""Rate Limiter — A177 RATE gate.

Token bucket rate limiter for the information-layer gateway.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

_DEFAULT_RATE_CAPACITY: Final[int] = 30
_DEFAULT_RATE_REFILL_PER_SEC: Final[float] = 10.0


class RateLimiter:
    """Token-bucket rate limiter per sender (A177 RATE gate)."""

    def __init__(
        self,
        capacity: int = _DEFAULT_RATE_CAPACITY,
        refill_per_sec: float = _DEFAULT_RATE_REFILL_PER_SEC,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        self._capacity = capacity
        self._refill_per_sec = refill_per_sec
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, sender: str) -> bool:
        """Check and consume one token for sender; return True if allowed."""
        now = time.monotonic()
        async with self._lock:
            tokens, last = self._buckets.get(sender, (self._capacity, now))
            # Refill based on elapsed time
            elapsed = now - last
            tokens = min(self._capacity, tokens + elapsed * self._refill_per_sec)
            if tokens >= 1.0:
                self._buckets[sender] = (tokens - 1.0, now)
                return True
            self._buckets[sender] = (tokens, now)
            return False


__all__ = ["RateLimiter"]