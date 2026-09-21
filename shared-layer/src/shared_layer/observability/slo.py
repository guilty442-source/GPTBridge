"""SQL Layer SLO Metrics (Blueprint Spec 15 / D10).

Formal SLO 指標 for the SQL layer:
- 中央查詢 p95
- transport claim latency
- reconcile backlog
- SQLite lock rate
- Qdrant stale rate
- restore success rate
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .metrics import MetricsCollector, get_metrics_collector, UnifiedMetrics


@dataclass
class SLOTarget:
    """SLO target definition."""
    name: str
    target_value: float
    unit: str
    description: str


class SQLSLOMetrics:
    """SQL Layer SLO metric definitions and collection."""

    # SLO targets per blueprint Spec 15 / D10
    TARGETS: dict[str, SLOTarget] = {
        "central_query_p95_ms": SLOTarget(
            name="central_query_p95_ms",
            target_value=100.0,
            unit="ms",
            description="Central PostgreSQL query p95 latency",
        ),
        "transport_claim_latency_p95_ms": SLOTarget(
            name="transport_claim_latency_p95_ms",
            target_value=50.0,
            unit="ms",
            description="Transport claim operation p95 latency",
        ),
        "reconcile_backlog_count": SLOTarget(
            name="reconcile_backlog_count",
            target_value=1000.0,
            unit="count",
            description="Pending SQLite→PostgreSQL reconciliation items",
        ),
        "sqlite_lock_rate": SLOTarget(
            name="sqlite_lock_rate",
            target_value=0.01,
            unit="ratio",
            description="SQLite lock contention rate (lock wait / total ops)",
        ),
        "qdrant_stale_rate": SLOTarget(
            name="qdrant_stale_rate",
            target_value=0.05,
            unit="ratio",
            description="Qdrant index stale rate (behind PG revision / total)",
        ),
        "restore_success_rate": SLOTarget(
            name="restore_success_rate",
            target_value=0.999,
            unit="ratio",
            description="Restore certification success rate",
        ),
    }

    def __init__(self, collector: MetricsCollector | None = None) -> None:
        self._collector = collector or get_metrics_collector()
        self._lock = threading.RLock()
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._start_times: dict[str, float] = {}

    def observe_query_latency(self, latency_ms: float, *, query_type: str = "central") -> None:
        """Observe a query latency for p95 calculation."""
        key = f"{query_type}_query_latency_ms"
        with self._lock:
            self._histograms[key].append(latency_ms)

    def observe_transport_claim(self, latency_ms: float) -> None:
        """Observe transport claim latency."""
        with self._lock:
            self._histograms["transport_claim_latency_ms"].append(latency_ms)

    def set_reconcile_backlog(self, count: int) -> None:
        """Set current reconcile backlog count."""
        with self._lock:
            self._gauges["reconcile_backlog_count"] = float(count)

    def observe_sqlite_lock(self, waited: bool) -> None:
        """Record SQLite lock wait (for rate calculation)."""
        with self._lock:
            self._counters["sqlite_lock_total"] += 1
            if waited:
                self._counters["sqlite_lock_waited"] += 1

    def set_qdrant_stale_rate(self, rate: float) -> None:
        """Set Qdrant stale rate."""
        with self._lock:
            self._gauges["qdrant_stale_rate"] = rate

    def record_restore_result(self, success: bool) -> None:
        """Record restore attempt result."""
        with self._lock:
            self._counters["restore_total"] += 1
            if success:
                self._counters["restore_success"] += 1

    def get_slo_status(self) -> dict[str, Any]:
        """Compute current SLO status against targets."""
        with self._lock:
            status = {}
            for key, target in self.TARGETS.items():
                current = self._compute_current(key)
                status[key] = {
                    "target": target.target_value,
                    "current": current,
                    "unit": target.unit,
                    "description": target.description,
                    "healthy": self._is_healthy(key, current, target.target_value),
                }
            return status

    def _compute_current(self, key: str) -> float:
        if key == "central_query_p95_ms":
            return self._percentile(self._histograms.get("central_query_latency_ms", []), 95)
        if key == "transport_claim_latency_p95_ms":
            return self._percentile(self._histograms.get("transport_claim_latency_ms", []), 95)
        if key == "reconcile_backlog_count":
            return self._gauges.get("reconcile_backlog_count", 0.0)
        if key == "sqlite_lock_rate":
            total = self._counters.get("sqlite_lock_total", 0)
            waited = self._counters.get("sqlite_lock_waited", 0)
            return waited / total if total > 0 else 0.0
        if key == "qdrant_stale_rate":
            return self._gauges.get("qdrant_stale_rate", 0.0)
        if key == "restore_success_rate":
            total = self._counters.get("restore_total", 0)
            success = self._counters.get("restore_success", 0)
            return success / total if total > 0 else 1.0
        return 0.0

    def _is_healthy(self, key: str, current: float, target: float) -> bool:
        if key in ("central_query_p95_ms", "transport_claim_latency_p95_ms", "reconcile_backlog_count"):
            return current <= target
        if key in ("sqlite_lock_rate", "qdrant_stale_rate"):
            return current <= target
        if key == "restore_success_rate":
            return current >= target
        return True

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int((p / 100) * (len(sorted_vals) - 1))
        return sorted_vals[idx]

    def export_to_collector(self, component: str = "sql-governance") -> None:
        """Export current SLO metrics to the global collector."""
        metrics = self._collector.register_component(
            component=component,
            component_type="runtime",
            version="1.0.0",
        )
        status = self.get_slo_status()
        for key, data in status.items():
            if key.endswith("_p95_ms") or key.endswith("_latency_p95_ms"):
                metrics.set_gauge(key, data["current"], unit=data["unit"])
            elif key.endswith("_rate") or key.endswith("_ratio"):
                metrics.set_gauge(key, data["current"], unit=data["unit"])
            else:
                metrics.set_gauge(key, data["current"], unit=data["unit"])
            # Also export health as gauge
            metrics.set_gauge(f"{key}_healthy", 1.0 if data["healthy"] else 0.0)


# Global instance
_sql_slo: SQLSLOMetrics | None = None
_sql_slo_lock = threading.Lock()


def get_sql_slo() -> SQLSLOMetrics:
    """Get or create the global SQL SLO metrics instance."""
    global _sql_slo
    with _sql_slo_lock:
        if _sql_slo is None:
            _sql_slo = SQLSLOMetrics()
        return _sql_slo


__all__ = [
    "SQLSLOMetrics",
    "SLOTarget",
    "get_sql_slo",
]