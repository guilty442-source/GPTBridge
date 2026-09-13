"""Resilient Shared Layer Store for main-system — facade.

This module provides the ResilientSharedLayerStore class.  Types,
circuit breaker, and helpers live in
:mod:`resilient_shared_layer_types`.

Connection stability with retry, circuit breaker, and health monitoring
around PostgresSharedLayerStore.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from shared_layer import SharedLayerStore as BaseSharedLayerStore
from shared_layer.database.connection import ConnectionManager
from shared_layer.database.config import DatabaseSettings
from governance_rule.execution.authentication import GovernanceAuthenticationService
from governance_rule.permission_directory.directory_authority import directory_authority_snapshot

from .resilient_shared_layer_types import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ConnectionStats,
    ResilientStoreConfig,
    _calculate_delay,
    _is_retryable,
)

_logger = logging.getLogger("gptbridge.resilient_shared_layer")

T = TypeVar("T")


class ResilientSharedLayerStore:
    """Resilient wrapper around PostgresSharedLayerStore with connection pooling."""

    def __init__(
        self,
        project_root: Any,
        authentication: GovernanceAuthenticationService,
        channel_id: str = "system",
        config: Optional[ResilientStoreConfig] = None,
    ) -> None:
        self._project_root = project_root
        self._authentication = authentication
        self._channel_id = str(channel_id or "").strip().casefold()
        self._config = config or ResilientStoreConfig()
        self._stats = ConnectionStats()
        self._circuit_breaker = CircuitBreaker(
            name=f"shared-layer-store-{self._channel_id}",
            failure_threshold=self._config.circuit_failure_threshold,
            recovery_timeout=self._config.circuit_recovery_timeout,
        ) if self._config.enable_circuit_breaker else None
        self._health_check_thread: Optional[threading.Thread] = None
        self._health_check_stop = threading.Event()
        self._last_health_check: float = 0.0
        self._health_check_ok: bool = True
        self._pool: Optional[ConnectionManager] = None
        self._pool_lock = threading.Lock()
        self._base_store: Optional[BaseSharedLayerStore] = None

    def _get_pool(self) -> ConnectionManager:
        """Get or create the connection pool."""
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    policy = directory_authority_snapshot().shared_layer_access_policy
                    declared = (
                        policy.ai_database_path
                        if self._channel_id == "ai"
                        else policy.database_path
                    )
                    if not declared.startswith("postgresql:"):
                        raise PermissionError("INVALID_DATABASE_PATH")
                    import os
                    from psycopg.conninfo import conninfo_to_dict, make_conninfo

                    values = conninfo_to_dict(os.environ.get("GPTBRIDGE_POSTGRES_DSN", ""))
                    if not values.get("dbname"):
                        values["dbname"] = "gptbridge"

                    settings = DatabaseSettings(
                        admin_dsn=make_conninfo(**values),
                        database="gptbridge",
                    )
                    self._pool = ConnectionManager(
                        settings,
                        min_size=self._config.pool_min_size,
                        max_size=self._config.pool_max_size,
                    )
                    self._pool.open()
                    _logger.info(
                        "connection_pool_opened channel=%s min=%d max=%d",
                        self._channel_id,
                        self._config.pool_min_size,
                        self._config.pool_max_size,
                    )
        return self._pool

    def _get_base_store(self) -> BaseSharedLayerStore:
        """Get or create the base store."""
        if self._base_store is None:
            self._base_store = BaseSharedLayerStore(
                self._project_root,
                self._authentication,
                self._channel_id,
            )
        return self._base_store

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
                self._stats.record_failure()

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
            self._get_base_store().submit_request,
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
            self._get_base_store().cancel_request,
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
            self._get_base_store().request_cancelled,
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
            self._get_base_store().consume_response,
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
            self._get_base_store().publish_progress,
            token, request_id, target_tool_id, progress
        )

    def claim_request(
        self,
        token: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        return self._execute_with_resilience(
            "claim_request",
            self._get_base_store().claim_request,
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
            self._get_base_store().respond,
            token, request_id, target_tool_id, response
        )

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        return self._execute_with_resilience(
            "notify_channel",
            self._get_base_store().notify_channel,
            token, target_tool_id
        )

    def status(self) -> dict[str, Any]:
        base_status = self._get_base_store().status()
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
        status["connection_pool"] = {
            "enabled": self._pool is not None,
            "min_size": self._config.pool_min_size,
            "max_size": self._config.pool_max_size,
        } if self._pool else {"enabled": False}
        return status

    def start_health_monitoring(self) -> None:
        """Start background health check monitoring."""
        if self._health_check_thread is not None and self._health_check_thread.is_alive():
            return
        self._health_check_stop.clear()
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop,
            daemon=True,
            name=f"health-check-shared-layer-{self._channel_id}",
        )
        self._health_check_thread.start()
        _logger.info("health_monitoring_started channel=%s", self._channel_id)

    def stop_health_monitoring(self) -> None:
        """Stop background health check monitoring."""
        self._health_check_stop.set()
        if self._health_check_thread is not None:
            self._health_check_thread.join(timeout=5.0)
            self._health_check_thread = None
        _logger.info("health_monitoring_stopped channel=%s", self._channel_id)

    def _health_check_loop(self) -> None:
        """Background health check loop."""
        while not self._health_check_stop.is_set():
            try:
                self._perform_health_check()
            except Exception as exc:
                _logger.error("health_check_failed error=%s", exc)
            self._health_check_stop.wait(self._config.health_check_interval)

    def _perform_health_check(self) -> None:
        """Perform a single health check using the connection pool."""
        try:
            pool = self._get_pool()
            with pool.connection() as conn:
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
            _logger.info("circuit_breaker_reset channel=%s", self._channel_id)

    def close(self) -> None:
        """Close the connection pool and stop health monitoring."""
        self.stop_health_monitoring()
        if self._pool is not None:
            with self._pool_lock:
                if self._pool is not None:
                    self._pool.close()
                    self._pool = None
                    _logger.info("connection_pool_closed channel=%s", self._channel_id)

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to base store."""
        return getattr(self._get_base_store(), name)


def create_resilient_store(
    project_root: Any,
    authentication: GovernanceAuthenticationService,
    channel_id: str = "system",
    **config_kwargs,
) -> ResilientSharedLayerStore:
    """Convenience function to create a resilient store with default config."""
    config = ResilientStoreConfig(**config_kwargs)
    return ResilientSharedLayerStore(project_root, authentication, channel_id, config)


__all__ = [
    "ResilientSharedLayerStore",
    "ResilientStoreConfig",
    "ConnectionStats",
    "CircuitBreaker",
    "CircuitState",
    "CircuitOpenError",
    "create_resilient_store",
]
