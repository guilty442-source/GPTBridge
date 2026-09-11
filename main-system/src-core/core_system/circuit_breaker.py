"""Circuit breaker — automatic fallback on consecutive failures.

Wraps a callable with failure tracking.  After ``failure_threshold``
consecutive failures, the circuit opens and calls are redirected to a
fallback (or raise ``CircuitOpenError``).  After ``recovery_timeout``
seconds, the circuit enters half-open: one trial call is allowed; if it
succeeds, the circuit closes; if it fails, it re-opens.

Usage::

    from core_system.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(
        name="hot-reload",
        failure_threshold=3,
        recovery_timeout_seconds=30,
        fallback_fn=lambda: {"ok": False, "error": "circuit-open"},
    )
    result = cb.call(reload_modules, governance=gov, approval_token=tok)

Decorator usage::

    @CircuitBreaker.decorate("my-service", failure_threshold=5)
    def my_function():
        ...
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, TypeVar

from startup_core.feature_flags import get_flags

_logger = logging.getLogger("gptbridge.circuit_breaker")

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing — reject calls
    HALF_OPEN = "half_open" # Trial — one call allowed


class CircuitOpenError(Exception):
    """Raised when the circuit is open and no fallback is provided."""


class CircuitBreaker:
    """Thread-safe circuit breaker with automatic recovery."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        fallback_fn: Callable[..., T] | None = None,
        flag_name: str | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.fallback_fn = fallback_fn
        self.flag_name = flag_name
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._total_calls = 0
        self._total_failures = 0
        self._total_fallbacks = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed.
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    _logger.info(
                        "circuit_half_open name=%s — trial call allowed",
                        self.name,
                    )
            return self._state

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call fn through the circuit breaker."""
        # Check if circuit breaker is enabled via feature flag.
        if self.flag_name is not None:
            flags = get_flags()
            if not flags.is_enabled(self.flag_name):
                return fn(*args, **kwargs)

        with self._lock:
            current_state = self.state
            if current_state == CircuitState.OPEN:
                self._total_fallbacks += 1
                if self.fallback_fn is not None:
                    _logger.warning(
                        "circuit_open_fallback name=%s total_fallbacks=%d",
                        self.name, self._total_fallbacks,
                    )
                    return self.fallback_fn(*args, **kwargs)
                raise CircuitOpenError(
                    f"circuit '{self.name}' is open "
                    f"(failures={self._failure_count})"
                )

            self._total_calls += 1

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as error:
            self._on_failure()
            if current_state == CircuitState.HALF_OPEN:
                # Trial failed — re-open.
                _logger.warning(
                    "circuit_half_open_failed name=%s error=%s",
                    self.name, error,
                )
            if self.fallback_fn is not None:
                return self.fallback_fn(*args, **kwargs)
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                _logger.info(
                    "circuit_closed name=%s — recovered after trial success",
                    self.name,
                )
            self._failure_count = 0

    def _on_failure(self) -> None:
        with self._lock:
            self._total_failures += 1
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                _logger.warning(
                    "circuit_opened name=%s failures=%d threshold=%d "
                    "total_calls=%d total_failures=%d",
                    self.name, self._failure_count, self.failure_threshold,
                    self._total_calls, self._total_failures,
                )

    def reset(self) -> None:
        """Manually reset the circuit to closed."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0

    def stats(self) -> dict[str, Any]:
        """Return current circuit breaker statistics."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "total_fallbacks": self._total_fallbacks,
                "recovery_timeout_seconds": self.recovery_timeout,
            }

    @staticmethod
    def decorate(
        name: str,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        fallback_fn: Callable[..., Any] | None = None,
        flag_name: str | None = None,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Decorator factory for wrapping a function with a circuit breaker."""
        def decorator(fn: Callable[..., T]) -> Callable[..., T]:
            cb = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout_seconds=recovery_timeout_seconds,
                fallback_fn=fallback_fn,
                flag_name=flag_name,
            )

            def wrapper(*args: Any, **kwargs: Any) -> T:
                return cb.call(fn, *args, **kwargs)

            wrapper.circuit_breaker = cb  # type: ignore[attr-defined]
            return wrapper

        return decorator


__all__ = ["CircuitBreaker", "CircuitOpenError", "CircuitState"]
