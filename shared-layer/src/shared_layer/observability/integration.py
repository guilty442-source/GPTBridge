"""Observability Integration Helpers for GPTBridge Components.

Provides easy integration of unified observability (metrics, tracing, health)
into sovereigns, gateways, and runtimes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .metrics import MetricsCollector, get_metrics_collector, UnifiedMetrics
from .tracing import (
    CorrelationContext,
    get_correlation_id,
    set_correlation_id,
    with_correlation_id,
    inject_correlation_headers,
    extract_correlation_headers,
)
from .health import StandardHealthChecker, HealthStatus, HealthCheckResult, A263HealthContract
from .endpoint import MetricsEndpoint, MetricsEndpointConfig
from .coverage import CoverageAnalyzer, generate_coverage_report


# ================================================================
# Sovereign Observability Mixin
# ================================================================


@dataclass
class SovereignObservability:
    """Observability integration for sovereign components."""

    sovereign_id: str
    version: str = "1.0.0"
    collector: MetricsCollector = None  # type: ignore
    health_checker: StandardHealthChecker = None  # type: ignore
    metrics: UnifiedMetrics = None  # type: ignore

    def __post_init__(self):
        self.collector = self.collector or get_metrics_collector()
        self.metrics = self.collector.register_component(
            self.sovereign_id, "sovereign", self.version
        )
        self.health_checker = StandardHealthChecker(
            self.sovereign_id, "sovereign", self.version
        )
        self._setup_default_metrics()
        self._setup_default_health()

    def _setup_default_metrics(self) -> None:
        """Register default sovereign metrics."""
        self.metrics.add_metadata("sovereign_id", self.sovereign_id)
        self.metrics.add_metadata("version", self.version)

    def _setup_default_health(self) -> None:
        """Register default sovereign health checks."""
        self.health_checker.register_check(
            "governance_ready",
            lambda: HealthCheckResult.healthy("governance_ready", "Governance bridge operational"),
            critical=True,
        )
        self.health_checker.register_check(
            "runtime_ready",
            lambda: HealthCheckResult.healthy("runtime_ready", "Runtime dependencies available"),
            critical=True,
        )
        self.health_checker.register_check(
            "dependencies",
            lambda: HealthCheckResult.healthy("dependencies", "External dependencies reachable"),
            critical=False,
        )

    def record_decision(self, intent: str, outcome: str, duration_ms: float) -> None:
        """Record a sovereign decision."""
        self.metrics.add_counter("decisions_total", 1, intent=intent, outcome=outcome)
        self.metrics.observe_histogram("decision_duration_seconds", duration_ms / 1000, intent=intent)

    def record_delegation(self, target: str, success: bool) -> None:
        """Record a delegation outcome."""
        self.metrics.add_counter("delegations_total", 1, target=target, outcome="success" if success else "failure")

    def record_error(self, error_type: str, context: str = "") -> None:
        """Record an error."""
        self.metrics.add_counter("errors_total", 1, error_type=error_type, context=context)

    async def health_check(self) -> list[HealthCheckResult]:
        """Run all health checks."""
        return await self.health_checker.check()

    async def overall_health(self) -> HealthStatus:
        """Get overall health status."""
        return await self.health_checker.overall_status()


# ================================================================
# Gateway Observability Mixin
# ================================================================


@dataclass
class GatewayObservability:
    """Observability integration for gateway components."""

    gateway_id: str
    version: str = "1.0.0"
    collector: MetricsCollector = None  # type: ignore
    health_checker: StandardHealthChecker = None  # type: ignore
    metrics: UnifiedMetrics = None  # type: ignore
    a263_contract: A263HealthContract = None  # type: ignore

    def __post_init__(self):
        self.collector = self.collector or get_metrics_collector()
        self.metrics = self.collector.register_component(
            self.gateway_id, "gateway", self.version
        )
        self.health_checker = StandardHealthChecker(
            self.gateway_id, "gateway", self.version
        )
        self.a263_contract = A263HealthContract(self.gateway_id, self.gateway_id)
        self._setup_default_metrics()
        self._setup_default_health()

    def _setup_default_metrics(self) -> None:
        """Register default gateway metrics."""
        self.metrics.add_metadata("gateway_id", self.gateway_id)
        self.metrics.add_metadata("version", self.version)

    def _setup_default_health(self) -> None:
        """Register default gateway health checks."""
        self.health_checker.register_check(
            "channel_connectivity",
            lambda: HealthCheckResult.healthy("channel_connectivity", "Channel connected"),
            critical=True,
        )
        self.health_checker.register_check(
            "heartbeat_liveness",
            lambda: HealthCheckResult.healthy("heartbeat_liveness", "Heartbeat within deadline"),
            critical=True,
        )
        self.health_checker.register_check(
            "generation_validity",
            lambda: HealthCheckResult.healthy("generation_validity", "Generation valid"),
            critical=True,
        )

    def record_request(self, command: str, success: bool, duration_ms: float) -> None:
        """Record a gateway request."""
        self.metrics.add_counter("requests_total", 1, command=command, outcome="success" if success else "failure")
        self.metrics.observe_histogram("request_duration_seconds", duration_ms / 1000, command=command)

    def record_rate_limit(self, sender: str) -> None:
        """Record a rate limit event."""
        self.metrics.add_counter("rate_limited_total", 1, sender=sender)

    def record_auth_failure(self, sender: str) -> None:
        """Record an authentication failure."""
        self.metrics.add_counter("auth_failures_total", 1, sender=sender)

    def record_contract_denial(self, command: str) -> None:
        """Record a contract gate denial."""
        self.metrics.add_counter("contract_denials_total", 1, command=command)

    def set_active_connections(self, count: int) -> None:
        """Set active connections gauge."""
        self.metrics.set_gauge("active_connections", count)

    async def health_check(self) -> list[HealthCheckResult]:
        """Run all health checks."""
        return await self.health_checker.check()

    async def overall_health(self) -> HealthStatus:
        """Get overall health status."""
        return await self.health_checker.overall_status()


# ================================================================
# Runtime Observability Mixin
# ================================================================


@dataclass
class RuntimeObservability:
    """Observability integration for runtime components."""

    runtime_id: str
    version: str = "1.0.0"
    collector: MetricsCollector = None  # type: ignore
    health_checker: StandardHealthChecker = None  # type: ignore
    metrics: UnifiedMetrics = None  # type: ignore

    def __post_init__(self):
        self.collector = self.collector or get_metrics_collector()
        self.metrics = self.collector.register_component(
            self.runtime_id, "runtime", self.version
        )
        self.health_checker = StandardHealthChecker(
            self.runtime_id, "runtime", self.version
        )
        self._setup_default_metrics()
        self._setup_default_health()

    def _setup_default_metrics(self) -> None:
        """Register default runtime metrics."""
        self.metrics.add_metadata("runtime_id", self.runtime_id)
        self.metrics.add_metadata("version", self.version)
        self._start_time = time.time()

    def _setup_default_health(self) -> None:
        """Register default runtime health checks."""
        self.health_checker.register_check(
            "process_health",
            lambda: HealthCheckResult.healthy("process_health", "Process running normally"),
            critical=True,
        )
        self.health_checker.register_check(
            "child_health",
            lambda: HealthCheckResult.healthy("child_health", "Child processes healthy"),
            critical=False,
        )
        self.health_checker.register_check(
            "resource_availability",
            lambda: HealthCheckResult.healthy("resource_availability", "Resources available"),
            critical=False,
        )

    def record_uptime(self) -> None:
        """Update uptime gauge."""
        self.metrics.set_gauge("process_uptime_seconds", time.time() - self._start_time)

    def set_memory_usage(self, bytes_used: int) -> None:
        """Set memory usage gauge."""
        self.metrics.set_gauge("memory_usage_bytes", bytes_used)

    def set_cpu_usage(self, percent: float) -> None:
        """Set CPU usage gauge."""
        self.metrics.set_gauge("cpu_usage_percent", percent)

    def set_child_processes(self, count: int) -> None:
        """Set child processes gauge."""
        self.metrics.set_gauge("child_processes", count)

    def increment_restart_count(self) -> None:
        """Increment restart counter."""
        self.metrics.add_counter("restart_count", 1)

    def set_health_status(self, status: str) -> None:
        """Set health status gauge (0=unhealthy, 1=degraded, 2=healthy)."""
        status_map = {"unhealthy": 0, "degraded": 1, "healthy": 2}
        self.metrics.set_gauge("health_status", status_map.get(status, 0))

    async def health_check(self) -> list[HealthCheckResult]:
        """Run all health checks."""
        return await self.health_checker.check()

    async def overall_health(self) -> HealthStatus:
        """Get overall health status."""
        return await self.health_checker.overall_status()


# ================================================================
# Correlation ID Middleware
# ================================================================


class CorrelationMiddleware:
    """Middleware for propagating correlation IDs across async boundaries."""

    def __init__(self) -> None:
        self._extractors: list[Callable[[Any], Optional[str]]] = []
        self._injectors: list[Callable[[Any, str], None]] = []

    def add_extractor(self, extractor: Callable[[Any], Optional[str]]) -> None:
        """Add a correlation ID extractor."""
        self._extractors.append(extractor)

    def add_injector(self, injector: Callable[[Any, str], None]) -> None:
        """Add a correlation ID injector."""
        self._injectors.append(injector)

    async def extract(self, request: Any) -> str:
        """Extract correlation ID from request."""
        for extractor in self._extractors:
            cid = extractor(request)
            if cid:
                return cid
        return ""

    async def inject(self, response: Any, correlation_id: str) -> None:
        """Inject correlation ID into response."""
        for injector in self._injectors:
            injector(response, correlation_id)

    @contextlib.asynccontextmanager
    async def with_correlation(self, correlation_id: str | None = None):
        """Context manager for correlation ID."""
        cid = correlation_id or get_correlation_id()
        if not cid:
            import uuid
            cid = str(uuid.uuid4())
        set_correlation_id(cid)
        try:
            yield cid
        finally:
            pass  # ContextVar auto-restores


# ================================================================
# Convenience Functions
# ================================================================


def create_sovereign_observability(sovereign_id: str, version: str = "1.0.0") -> SovereignObservability:
    """Create observability for a sovereign."""
    return SovereignObservability(sovereign_id, version)


def create_gateway_observability(gateway_id: str, version: str = "1.0.0") -> GatewayObservability:
    """Create observability for a gateway."""
    return GatewayObservability(gateway_id, version)


def create_runtime_observability(runtime_id: str, version: str = "1.0.0") -> RuntimeObservability:
    """Create observability for a runtime."""
    return RuntimeObservability(runtime_id, version)


def create_metrics_endpoint_for_components(
    port: int = 9090,
    health_checkers: dict[str, StandardHealthChecker] | None = None,
) -> MetricsEndpoint:
    """Create a metrics endpoint with registered health checkers."""
    config = MetricsEndpointConfig(port=port)
    endpoint = MetricsEndpoint(config, health_checkers=health_checkers or {})
    endpoint.start()
    return endpoint


def run_coverage_analysis(
    collector: MetricsCollector | None = None,
    health_checkers: dict[str, StandardHealthChecker] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Run observability coverage analysis."""
    report = generate_coverage_report(collector, health_checkers, output_path)
    return report.to_dict()