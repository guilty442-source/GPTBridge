"""Unified Metrics Format for GPTBridge.

Standardizes metric collection, formatting, and export across all components.
Compatible with Prometheus exposition format for /metrics endpoint.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Final

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

# ================================================================
# Metric Types and Values
# ================================================================


class MetricType(Enum):
    """Standard metric types per Prometheus/OpenMetrics."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


@dataclass(frozen=True)
class MetricValue:
    """Immutable metric value with metadata."""

    value: float
    timestamp: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict)

    def with_labels(self, **labels: str) -> Self:
        """Return new MetricValue with additional labels."""
        merged = {**self.labels, **labels}
        return MetricValue(self.value, self.timestamp, merged)


# ================================================================
# Unified Metrics Data Structure
# ================================================================


@dataclass
class UnifiedMetrics:
    """Unified metrics container for a component.

    All sovereign/gateway/runtime components export metrics in this format.
    """

    component: str
    component_type: str  # "sovereign" | "gateway" | "runtime" | "tool"
    version: str
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    metadata: dict[str, Any] = field(default_factory=dict)
    collected_at: float = field(default_factory=time.time)

    def add_counter(self, name: str, value: float = 1.0, **labels: str) -> Self:
        """Add/increment a counter metric."""
        key = self._metric_key(name, labels)
        existing = self.metrics.get(key)
        new_value = (existing.value if existing else 0.0) + value
        self.metrics[key] = MetricValue(new_value, labels=labels)
        return self

    def set_gauge(self, name: str, value: float, **labels: str) -> Self:
        """Set a gauge metric value."""
        key = self._metric_key(name, labels)
        self.metrics[key] = MetricValue(value, labels=labels)
        return self

    def observe_histogram(self, name: str, value: float, **labels: str) -> Self:
        """Record a histogram observation."""
        key = self._metric_key(name, labels)
        self.histograms[key].append(value)
        # Also keep latest as gauge for compatibility
        self.metrics[key] = MetricValue(value, labels=labels)
        return self

    def add_metadata(self, key: str, value: Any) -> Self:
        """Add component metadata."""
        self.metadata[key] = value
        return self

    def to_prometheus(self) -> str:
        """Export metrics in Prometheus exposition format."""
        lines: list[str] = []
        lines.append(f'# HELP gptbridge_component_info Component metadata')
        lines.append(f'# TYPE gptbridge_component_info gauge')
        lines.append(
            f'gptbridge_component_info{{component="{self.component}",type="{self.component_type}",version="{self.version}"}} 1'
        )

        # Counters and gauges
        for key, metric in self.metrics.items():
            name, labels = self._parse_key(key)
            labels_str = self._format_labels(labels)
            metric_type = self._infer_type(name)
            lines.append(f"# HELP {name} {metric_type.value} metric")
            lines.append(f"# TYPE {name} {metric_type.value}")
            lines.append(f"{name}{labels_str} {metric.value}")

        # Histograms (export as summary with quantiles)
        for key, values in self.histograms.items():
            if not values:
                continue
            name, labels = self._parse_key(key)
            labels_str = self._format_labels(labels)
            lines.append(f"# HELP {name} histogram metric")
            lines.append(f"# TYPE {name} summary")
            count = len(values)
            sum_val = sum(values)
            lines.append(f"{name}_count{labels_str} {count}")
            lines.append(f"{name}_sum{labels_str} {sum_val}")
            # Quantiles
            sorted_vals = sorted(values)
            for q in (0.5, 0.9, 0.95, 0.99):
                idx = int(q * (count - 1))
                lines.append(
                    f'{name}{{quantile="{q}",{labels_str.lstrip("{").rstrip("}")}}} {sorted_vals[idx]}'
                )

        lines.append(f"# COLLECTED_AT {datetime.fromtimestamp(self.collected_at, tz=timezone.utc).isoformat()}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Export as dictionary for JSON serialization."""
        return {
            "component": self.component,
            "component_type": self.component_type,
            "version": self.version,
            "metrics": {
                k: {"value": v.value, "timestamp": v.timestamp, "labels": v.labels}
                for k, v in self.metrics.items()
            },
            "histograms": {k: list(v) for k, v in self.histograms.items()},
            "metadata": self.metadata,
            "collected_at": self.collected_at,
        }

    @staticmethod
    def _metric_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str, dict[str, str]]:
        if "{" not in key:
            return key, {}
        name, rest = key.split("{", 1)
        labels = {}
        for pair in rest.rstrip("}").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k] = v.strip('"')
        return name, labels

    @staticmethod
    def _infer_type(name: str) -> MetricType:
        if name.endswith("_total") or name.endswith("_count"):
            return MetricType.COUNTER
        if name.endswith("_bucket") or "histogram" in name:
            return MetricType.HISTOGRAM
        return MetricType.GAUGE

    @staticmethod
    def _format_labels(labels: dict[str, str]) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


# ================================================================
# Metrics Collector
# ================================================================


class MetricsCollector:
    """Centralized metrics collector aggregating from all components."""

    def __init__(self) -> None:
        self._components: dict[str, UnifiedMetrics] = {}
        self._lock = threading.RLock()
        self._global_labels: dict[str, str] = {}

    def register_component(
        self,
        component: str,
        component_type: str,
        version: str,
    ) -> UnifiedMetrics:
        """Register a new component and return its metrics container."""
        with self._lock:
            metrics = UnifiedMetrics(component, component_type, version)
            self._components[component] = metrics
            return metrics

    def get_component(self, component: str) -> UnifiedMetrics | None:
        """Get metrics for a specific component."""
        with self._lock:
            return self._components.get(component)

    def unregister_component(self, component: str) -> None:
        """Unregister a component."""
        with self._lock:
            self._components.pop(component, None)

    def set_global_labels(self, **labels: str) -> None:
        """Set labels applied to all exported metrics."""
        with self._lock:
            self._global_labels.update(labels)

    def collect_all(self) -> dict[str, UnifiedMetrics]:
        """Collect metrics from all registered components."""
        with self._lock:
            return dict(self._components)

    def export_prometheus(self) -> str:
        """Export all metrics in Prometheus format."""
        with self._lock:
            lines: list[str] = []
            lines.append("# GPTBridge Unified Metrics Export")
            lines.append(f"# Generated at {datetime.now(timezone.utc).isoformat()}")
            lines.append("")

            for component, metrics in sorted(self._components.items()):
                lines.append(f"# Component: {component} ({metrics.component_type})")
                lines.append(metrics.to_prometheus())

            return "\n".join(lines)

    def export_json(self) -> dict[str, Any]:
        """Export all metrics as JSON."""
        with self._lock:
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "components": {
                    name: metrics.to_dict()
                    for name, metrics in self._components.items()
                },
                "global_labels": self._global_labels,
            }


# ================================================================
# Global Collector Instance
# ================================================================

_collector: MetricsCollector | None = None
_collector_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Get or create the global metrics collector."""
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = MetricsCollector()
        return _collector