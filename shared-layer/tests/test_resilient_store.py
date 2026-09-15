"""Tests for resilient_store module."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared_layer.resilient_store import (
    CircuitBreaker,
    CircuitState,
    CircuitOpenError,
    ResilientPostgresStore,
    ResilientStoreConfig,
    wrap_store,
)


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        cb._on_failure()
        cb._on_failure()
        assert cb.state == CircuitState.OPEN

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb._on_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

    def test_closes_on_success_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb._on_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb._on_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb._on_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb._on_failure()
        assert cb.state == CircuitState.OPEN

    def test_fallback_on_open(self):
        fallback = MagicMock(return_value="fallback")
        cb = CircuitBreaker("test", failure_threshold=1, fallback_fn=fallback)
        cb._on_failure()
        result = cb.call(lambda: "success")
        assert result == "fallback"
        fallback.assert_called_once()

    def test_raises_without_fallback(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        cb._on_failure()
        with pytest.raises(CircuitOpenError):
            cb.call(lambda: "success")

    def test_reset(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        cb._on_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED


class TestResilientPostgresStore:
    @pytest.fixture
    def mock_base_store(self):
        store = MagicMock()
        store._channel_id = "system"
        return store

    @pytest.fixture
    def config(self):
        return ResilientStoreConfig(
            max_retries=2,
            base_delay=0.01,
            max_delay=0.1,
            circuit_failure_threshold=2,
            circuit_recovery_timeout=0.1,
            enable_retry=True,
            enable_circuit_breaker=True,
        )

    def test_wraps_submit_request(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        resilient.submit_request("token", "req-1", "tool-1", {"key": "value"})
        mock_base_store.submit_request.assert_called_once_with("token", "req-1", "tool-1", {"key": "value"})

    def test_retries_on_transient_failure(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = [
            ConnectionError("connection lost"),
            ConnectionError("connection lost"),
            None,
        ]
        resilient.submit_request("token", "req-1", "tool-1", {})
        assert mock_base_store.submit_request.call_count == 3
        assert resilient._stats.retried_calls == 2

    def test_gives_up_after_max_retries(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = ConnectionError("connection lost")
        with pytest.raises(ConnectionError):
            resilient.submit_request("token", "req-1", "tool-1", {})
        assert mock_base_store.submit_request.call_count == 3  # initial + 2 retries

    def test_non_retryable_exception_not_retried(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = ValueError("bad data")
        with pytest.raises(ValueError):
            resilient.submit_request("token", "req-1", "tool-1", {})
        assert mock_base_store.submit_request.call_count == 1

    def test_circuit_breaker_opens_after_failures(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = ConnectionError("connection lost")

        # First two calls fail, opening circuit
        for _ in range(2):
            with pytest.raises(ConnectionError):
                resilient.submit_request("token", "req-1", "tool-1", {})

        # Third call should fail fast due to open circuit
        with pytest.raises(CircuitOpenError):
            resilient.submit_request("token", "req-1", "tool-1", {})

        assert resilient._stats.circuit_breaker_opens == 1

    def test_circuit_breaker_fallback(self, mock_base_store):
        fallback = MagicMock(return_value={"ok": False, "fallback": True})
        config = ResilientStoreConfig(
            max_retries=2,
            base_delay=0.01,
            max_delay=0.1,
            circuit_failure_threshold=2,
            circuit_recovery_timeout=0.1,
            enable_retry=True,
            enable_circuit_breaker=True,
            fallback_fn=fallback,
        )
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = ConnectionError("connection lost")

        # First two calls fail and retry (total 3 attempts each)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                resilient.submit_request("token", "req-1", "tool-1", {})

        # Third call: circuit is open, uses fallback
        result = resilient.submit_request("token", "req-1", "tool-1", {})
        assert result == {"ok": False, "fallback": True}
        assert resilient._stats.circuit_breaker_fallbacks == 1

    def test_stats_tracking(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)

        # Successful call
        mock_base_store.submit_request.return_value = None
        resilient.submit_request("token", "req-1", "tool-1", {})

        stats = resilient.get_resilience_status()
        assert stats["total_calls"] == 1
        assert stats["successful_calls"] == 1
        assert stats["failed_calls"] == 0

        # Failed call
        mock_base_store.submit_request.side_effect = ConnectionError("connection lost")
        with pytest.raises(ConnectionError):
            resilient.submit_request("token", "req-2", "tool-1", {})

        stats = resilient.get_resilience_status()
        assert stats["total_calls"] == 2
        assert stats["successful_calls"] == 1
        assert stats["failed_calls"] == 1

    def test_health_monitoring(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=MagicMock())
        mock_context.__exit__ = MagicMock(return_value=None)
        mock_base_store._connect.return_value = mock_context

        resilient.start_health_monitoring()
        time.sleep(0.05)
        resilient.stop_health_monitoring()

        assert resilient._last_health_check > 0
        assert resilient._health_check_ok is True

    def test_reset_circuit_breaker(self, mock_base_store, config):
        resilient = ResilientPostgresStore(mock_base_store, config)
        mock_base_store.submit_request.side_effect = ConnectionError("connection lost")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                resilient.submit_request("token", "req-1", "tool-1", {})

        assert resilient._circuit_breaker.state == CircuitState.OPEN
        resilient.reset_circuit_breaker()
        assert resilient._circuit_breaker.state == CircuitState.CLOSED

    def test_wrap_store_convenience(self, mock_base_store):
        resilient = wrap_store(mock_base_store, max_retries=1, base_delay=0.01)
        assert isinstance(resilient, ResilientPostgresStore)
        assert resilient._config.max_retries == 1


class TestResilientStoreConfig:
    def test_defaults(self):
        config = ResilientStoreConfig()
        assert config.max_retries == 3
        assert config.base_delay == 0.1
        assert config.max_delay == 2.0
        assert config.circuit_failure_threshold == 5
        assert config.circuit_recovery_timeout == 30.0
        assert config.enable_circuit_breaker is True
        assert config.enable_retry is True

    def test_custom_values(self):
        config = ResilientStoreConfig(max_retries=5, base_delay=0.5)
        assert config.max_retries == 5
        assert config.base_delay == 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])