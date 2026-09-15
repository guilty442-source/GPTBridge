"""Gateway Metrics — A177 observability gate.

Metrics collection for the InformationChannelGateway.
"""

from __future__ import annotations

import threading
from typing import Any


class GatewayMetrics:
    """Metrics tracking for InformationChannelGateway."""

    def __init__(self) -> None:
        self._metrics = {
            "requests_total": 0,
            "requests_allowed": 0,
            "requests_denied": 0,
            "contract_gate_failures": 0,
            "authorization_failures": 0,
            "rate_limit_exceeded": 0,
            "audit_failures": 0,
            "handler_errors": 0,
            "verification_failures": 0,
            "latency_sum_ms": 0.0,
            "latency_count": 0,
        }
        self._lock = threading.Lock()

    def record_request(self) -> None:
        with self._lock:
            self._metrics["requests_total"] += 1

    def record_allowed(self) -> None:
        with self._lock:
            self._metrics["requests_allowed"] += 1

    def record_denied(self, reason: str = "") -> None:
        with self._lock:
            self._metrics["requests_denied"] += 1
            if reason == "contract":
                self._metrics["contract_gate_failures"] += 1
            elif reason == "authorization":
                self._metrics["authorization_failures"] += 1
            elif reason == "rate_limit":
                self._metrics["rate_limit_exceeded"] += 1

    def record_audit_failure(self) -> None:
        with self._lock:
            self._metrics["audit_failures"] += 1

    def record_handler_error(self) -> None:
        with self._lock:
            self._metrics["handler_errors"] += 1

    def record_verification_failure(self) -> None:
        with self._lock:
            self._metrics["verification_failures"] += 1

    def record_latency(self, elapsed_ms: float) -> None:
        with self._lock:
            self._metrics["latency_sum_ms"] += elapsed_ms
            self._metrics["latency_count"] += 1

    def get_metrics(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._metrics)

    def get_metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            metrics = dict(self._metrics)
            if metrics["latency_count"] > 0:
                metrics["avg_latency_ms"] = metrics["latency_sum_ms"] / metrics["latency_count"]
            else:
                metrics["avg_latency_ms"] = 0.0
            return metrics


__all__ = ["GatewayMetrics"]