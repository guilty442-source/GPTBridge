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

from .resilient_circuit import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ConnectionStats,
    ResilientStoreConfig,
    _calculate_delay,
    _is_retryable,
)

T = TypeVar("T")
















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
        """Perform a single health check.

        The base store may expose either a per-call connection factory
        (``_connect``) or a connection pool (``_get_pool``); probe whichever
        exists so the health check reflects the real store.
        """
        try:
            base = self._base_store
            connect = getattr(base, "_connect", None)
            if callable(connect):
                with connect() as conn:
                    conn.execute("SELECT 1")
            else:
                pool = base._get_pool()
                with pool.acquire() as conn:
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