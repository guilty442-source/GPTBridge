"""Unified Observability Module for GPTBridge.

Provides standardized metrics, tracing, and health check interfaces
across all sovereign/gateway/runtime components.
"""

from __future__ import annotations

from .metrics import (
    MetricType,
    MetricValue,
    UnifiedMetrics,
    MetricsCollector,
    get_metrics_collector,
)
from .tracing import (
    CorrelationContext,
    get_correlation_id,
    set_correlation_id,
    clear_correlation_id,
    with_correlation_id,
)
from .health import (
    HealthStatus,
    HealthCheckResult,
    HealthChecker,
    StandardHealthChecker,
    A263HealthContract,
)
from .endpoint import (
    MetricsEndpoint,
    MetricsEndpointConfig,
    create_metrics_endpoint,
)
from .coverage import (
    CoverageDimension,
    ComponentCategory,
    CoverageCheck,
    ComponentCoverage,
    CoverageReport,
    CoverageAnalyzer,
    generate_coverage_report,
)
from .integration import (
    SovereignObservability,
    GatewayObservability,
    RuntimeObservability,
    CorrelationMiddleware,
    create_sovereign_observability,
    create_gateway_observability,
    create_runtime_observability,
    create_metrics_endpoint_for_components,
    run_coverage_analysis,
)

__all__ = [
    # Metrics
    "MetricType",
    "MetricValue",
    "UnifiedMetrics",
    "MetricsCollector",
    "get_metrics_collector",
    # Tracing
    "CorrelationContext",
    "get_correlation_id",
    "set_correlation_id",
    "clear_correlation_id",
    "with_correlation_id",
    # Health
    "HealthStatus",
    "HealthCheckResult",
    "HealthChecker",
    "StandardHealthChecker",
    "A263HealthContract",
    # Endpoint
    "MetricsEndpoint",
    "MetricsEndpointConfig",
    "create_metrics_endpoint",
    # Coverage
    "CoverageDimension",
    "ComponentCategory",
    "CoverageCheck",
    "ComponentCoverage",
    "CoverageReport",
    "CoverageAnalyzer",
    "generate_coverage_report",
    # Integration
    "SovereignObservability",
    "GatewayObservability",
    "RuntimeObservability",
    "CorrelationMiddleware",
    "create_sovereign_observability",
    "create_gateway_observability",
    "create_runtime_observability",
    "create_metrics_endpoint_for_components",
    "run_coverage_analysis",
]