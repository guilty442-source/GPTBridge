"""Resilient store wrapper — connection stability with retry, circuit breaker, and health monitoring.

This module provides a non-invasive wrapper around PostgresSharedLayerStore that adds:
- Exponential backoff retry logic for transient failures
- Circuit breaker pattern to prevent cascade failures
- Connection health monitoring and automatic reconnection
- Statistics collection for observability

Usage:
    from shared_layer.resilient_store import ResilientPostgresStore, ResilientStoreConfig

    config = ResilientStoreConfig(
        max_retries=3,
        base_delay=0.1,
        max_delay=2.0,
        circuit_failure_threshold=5,
        circuit_recovery_timeout=30.0,
    )
    resilient_store = ResilientPostgresStore(base_store, config)

    # Use exactly like PostgresSharedLayerStore
    resilient_store.submit_request(token, request_id, tool_id, payload)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from .store import PostgresSharedLayerStore

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


class ResilientPostgresStore:
    """Wrapper around PostgresSharedLayerStore adding connection stability features.

    This wrapper does not modify the underlying store - it adds resilience
    through composition. All public methods of PostgresSharedLayerStore are
    available with the same signatures.
    """

    def __init__(
        self,
        base_store: PostgresSharedLayerStore,
        config: Optional[ResilientStoreConfig] = None,
    ) -> None:
        self._base_store = base_store
        self._config = config or ResilientStoreConfig()
        self._stats = ConnectionStats()
        self._circuit_breaker = CircuitBreaker(
            name=f"postgres-store-{base_store._channel_id}",
            failure_threshold=self._config.circuit_failure_threshold,
            recovery_timeout=self._config.circuit_recovery_timeout,
            fallback_fn=self._config.fallback_fn if hasattr(self._config, 'fallback_fn') else None,
            on_open=lambda: self._stats.record_circuit_open(),
            on_fallback=lambda: self._stats.record_fallback(),
        ) if self._config.enable_circuit_breaker else None
        self._health_check_thread: Optional[threading.Thread] = None
        self._health_check_stop = threading.Event()
        self._last_health_check: float = 0.0
        self._health_check_ok: bool = True

    def _execute_with_resilience(
        self,
        operation_name: str,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any
    ) -> T:
        """Execute a function with retry and circuit breaker."""
        if self._circuit_breaker:
            return self._circuit_breaker.call(self._execute_with_retry, operation_name, fn, *args, **kwargs)
        return self._execute_with_retry(operation_name, fn, *args, **kwargs)

    def _execute_with_retry(
        self,
        operation_name: str,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any
    ) -> T:
        """Execute a function with exponential backoff retry."""
        last_exception: Optional[Exception] = None
        max_attempts = self._config.max_retries + 1 if self._config.enable_retry else 1

        for attempt in range(max_attempts):
            try:
                result = fn(*args, **kwargs)
                self._stats.record_success()
                return result
            except Exception as exc:
                last_exception = exc

                if not self._config.enable_retry or attempt >= self._config.max_retries:
                    break

                if not _is_retryable(exc, self._config):
                    _logger.warning(
                        "non_retryable_error operation=%s error=%s",
                        operation_name, exc
                    )
                    break

                delay = _calculate_delay(attempt, self._config)
                _logger.warning(
                    "retrying operation=%s attempt=%d/%d delay=%.3f error=%s",
                    operation_name, attempt + 1, max_attempts, delay, exc
                )
                self._stats.record_retry()
                time.sleep(delay)

        self._stats.record_failure()
        raise last_exception

    # Delegate all store methods with resilience
    def submit_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        payload: Any,
    ) -> None:
        return self._execute_with_resilience(
            "submit_request",
            self._base_store.submit_request,
            token, request_id, target_tool_id, payload
        )

    def cancel_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        return self._execute_with_resilience(
            "cancel_request",
            self._base_store.cancel_request,
            token, request_id, target_tool_id
        )

    def request_cancelled(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        return self._execute_with_resilience(
            "request_cancelled",
            self._base_store.request_cancelled,
            token, request_id, target_tool_id
        )

    def consume_response(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        return self._execute_with_resilience(
            "consume_response",
            self._base_store.consume_response,
            token, request_id, target_tool_id
        )

    def publish_progress(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        progress: Any,
    ) -> bool:
        return self._execute_with_resilience(
            "publish_progress",
            self._base_store.publish_progress,
            token, request_id, target_tool_id, progress
        )

    def claim_request(
        self,
        token: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        return self._execute_with_resilience(
            "claim_request",
            self._base_store.claim_request,
            token, target_tool_id
        )

    def respond(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        response: Any,
    ) -> bool:
        return self._execute_with_resilience(
            "respond",
            self._base_store.respond,
            token, request_id, target_tool_id, response
        )

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        return self._execute_with_resilience(
            "notify_channel",
            self._base_store.notify_channel,
            token, target_tool_id
        )

    def status(self) -> dict[str, Any]:
        base_status = self._base_store.status()
        base_status["resilience"] = self.get_resilience_status()
        return base_status

    def get_resilience_status(self) -> dict[str, Any]:
        """Get resilience statistics and health status."""
        status = self._stats.as_dict()
        status["circuit_breaker"] = (
            self._circuit_breaker.stats() if self._circuit_breaker else {"enabled": False}
        )
        status["health_check"] = {
            "last_check": self._last_health_check,
            "ok": self._health_check_ok,
            "interval_seconds": self._config.health_check_interval,
        }
        return status

    def start_health_monitoring(self) -> None:
        """Start background health check monitoring."""
        if self._health_check_thread is not None and self._health_check_thread.is_alive():
            return
        self._health_check_stop.clear()
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name=f"health-check-{self._base_store._channel_id}",
        )
        self._health_check_thread.start()
        _logger.info("health_monitoring_started channel=%s", self._base_store._channel_id)

    def stop_health_monitoring(self) -> None:
        """Stop background health check monitoring."""
        self._health_check_stop.set()
        if self._health_check_thread is not None:
            self._health_check_thread.join(timeout=5.0)
            self._health_check_thread = None
        _logger.info("health_monitoring_stopped channel=%s", self._base_store._channel_id)

    def _health_check_loop(self) -> None:
        """Background health check loop."""
        while not self._health_check_stop.is_set():
            try:
                self._perform_health_check()
            except Exception as exc:
                _logger.error("health_check_failed error=%s", exc)
            self._health_check_stop.wait(self._config.health_check_interval)

    def _perform_health_check(self) -> None:
        """Perform a single health check."""
        try:
            with self._base_store._connect() as conn:
                conn.execute("SELECT 1")
            self._health_check_ok = True
        except Exception as exc:
            self._health_check_ok = False
            _logger.warning("health_check_unhealthy error=%s", exc)
        finally:
            self._last_health_check = time.monotonic()

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        if self._circuit_breaker:
            self._circuit_breaker.reset()
            _logger.info("circuit_breaker_reset channel=%s", self._base_store._channel_id)

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to base store."""
        return getattr(self._base_store, name)


def wrap_store(base_store: PostgresSharedLayerStore, **config_kwargs) -> ResilientPostgresStore:
    """Convenience function to wrap a store with default resilience config."""
    config = ResilientStoreConfig(**config_kwargs)
    return ResilientPostgresStore(base_store, config)


__all__ = [
    "ResilientPostgresStore",
    "ResilientStoreConfig",
    "ConnectionStats",
    "CircuitBreaker",
    "CircuitState",
    "CircuitOpenError",
    "wrap_store",
]