"""Circuit breaker, retry policy, and statistics primitives for the resilient store."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

_logger = logging.getLogger("gptbridge.resilient_store")

T = TypeVar("T")

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ResilientStoreConfig:
    """Configuration for resilient store behavior."""

    max_retries: int = 3
    base_delay: float = 0.1
    max_delay: float = 2.0
    exponential_base: float = 2.0
    jitter: float = 0.05
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 30.0
    health_check_interval: float = 60.0
    enable_circuit_breaker: bool = True
    enable_retry: bool = True
    retryable_exceptions: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
    )
    fallback_fn: Optional[Callable[..., Any]] = None


@dataclass
class ConnectionStats:
    """Connection statistics for observability."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    retried_calls: int = 0
    circuit_breaker_opens: int = 0
    circuit_breaker_fallbacks: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    consecutive_failures: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def record_success(self) -> None:
        with self._lock:
            self.total_calls += 1
            self.successful_calls += 1
            self.consecutive_failures = 0
            self.last_success_time = time.monotonic()

    def record_failure(self) -> None:
        with self._lock:
            self.total_calls += 1
            self.failed_calls += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()

    def record_retry(self) -> None:
        with self._lock:
            self.retried_calls += 1

    def record_circuit_open(self) -> None:
        with self._lock:
            self.circuit_breaker_opens += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.circuit_breaker_fallbacks += 1

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_calls": self.total_calls,
                "successful_calls": self.successful_calls,
                "failed_calls": self.failed_calls,
                "retried_calls": self.retried_calls,
                "circuit_breaker_opens": self.circuit_breaker_opens,
                "circuit_breaker_fallbacks": self.circuit_breaker_fallbacks,
                "last_failure_time": self.last_failure_time,
                "last_success_time": self.last_success_time,
                "consecutive_failures": self.consecutive_failures,
            }


class CircuitBreaker:
    """Thread-safe circuit breaker with automatic recovery."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        fallback_fn: Optional[Callable[..., T]] = None,
        on_open: Optional[Callable[[], None]] = None,
        on_fallback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.fallback_fn = fallback_fn
        self._on_open = on_open
        self._on_fallback = on_fallback
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    _logger.info(
                        "circuit_half_open name=%s — trial call allowed",
                        self.name,
                    )
            return self._state

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            current_state = self.state
            if current_state == CircuitState.OPEN:
                if self.fallback_fn is not None:
                    _logger.warning(
                        "circuit_open_fallback name=%s", self.name
                    )
                    if self._on_fallback is not None:
                        try:
                            self._on_fallback()
                        except Exception:
                            pass
                    return self.fallback_fn(*args, **kwargs)
                raise CircuitOpenError(
                    f"circuit '{self.name}' is open (failures={self._failure_count})"
                )

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as error:
            self._on_failure()
            if current_state == CircuitState.HALF_OPEN:
                _logger.warning(
                    "circuit_half_open_failed name=%s error=%s",
                    self.name, error,
                )
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                _logger.info(
                    "circuit_closed name=%s — recovered after trial success",
                    self.name,
                )

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                _logger.warning(
                    "circuit_opened name=%s failures=%d threshold=%d",
                    self.name, self._failure_count, self.failure_threshold,
                )
                if self._on_open is not None:
                    try:
                        self._on_open()
                    except Exception:
                        pass  # Callback must not affect circuit breaker

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout_seconds": self.recovery_timeout,
            }


class CircuitOpenError(Exception):
    """Raised when the circuit is open and no fallback is provided."""
    pass


def _calculate_delay(attempt: int, config: ResilientStoreConfig) -> float:
    """Calculate delay with exponential backoff and jitter."""
    delay = min(
        config.base_delay * (config.exponential_base ** attempt),
        config.max_delay
    )
    jitter_amount = delay * config.jitter
    import random
    return delay + random.uniform(-jitter_amount, jitter_amount)


def _is_retryable(exception: Exception, config: ResilientStoreConfig) -> bool:
    """Check if an exception is retryable."""
    return isinstance(exception, config.retryable_exceptions)
