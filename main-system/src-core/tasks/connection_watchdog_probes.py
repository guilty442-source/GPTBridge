"""Connection watchdog — probe infrastructure (A430 sub-module).

Extracted from ``connection_watchdog.py``: ``ProbeResult``,
``HealthCheckCache``, ``CircuitBreaker`` and ``ResourceUsageSnapshot``
are single-responsibility helpers that do not depend on the
``ConnectionWatchdog`` class.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

_logger = logging.getLogger("gptbridge.connection_watchdog")


class ProbeResult:
    """Result of a single health probe with metadata."""

    def __init__(
        self,
        success: bool,
        latency_ms: float,
        error: Optional[str] = None,
        error_type: Optional[str] = None,
    ):
        self.success = success
        self.latency_ms = latency_ms
        self.error = error
        self.error_type = error_type
        self.timestamp = time.monotonic()


class HealthCheckCache:
    """Thread-safe cache for health check results with TTL."""

    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[ProbeResult]:
        with self._lock:
            if key in self._cache:
                cached_time, result = self._cache[key]
                if time.monotonic() - cached_time < self.ttl_seconds:
                    return result
                else:
                    del self._cache[key]
        return None

    def set(self, key: str, result: ProbeResult) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), result)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


class CircuitBreaker:
    """Simple circuit breaker for external dependencies."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        fallback_fn: Optional[Callable[[], Any]] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.fallback_fn = fallback_fn
        self._lock = threading.RLock()
        self._state = "closed"  # closed, open, half_open
        self._failure_count = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open":
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = "half_open"
                    _logger.info("circuit_half_open name=%s", self.name)
            return self._state

    def call(self, fn: Callable[[], T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            if self.state == "open":
                if self.fallback_fn:
                    return self.fallback_fn()
                raise Exception(f"Circuit {self.name} is open")

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            if self.fallback_fn:
                return self.fallback_fn()
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == "half_open":
                self._state = "closed"

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = "open"

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0


@dataclass
class ResourceUsageSnapshot:
    """Snapshot of resource usage for leak detection."""
    timestamp: float
    thread_count: int
    open_files: int
    memory_mb: float
    connection_count: int


__all__ = [
    "CircuitBreaker",
    "HealthCheckCache",
    "ProbeResult",
    "ResourceUsageSnapshot",
]
