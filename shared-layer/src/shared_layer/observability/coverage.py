"""Observability Coverage Reporting.

Reports on observability coverage across all components, identifying gaps
in metrics, tracing, and health check instrumentation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .metrics import MetricsCollector, get_metrics_collector
from .health import HealthChecker, HealthStatus


class CoverageDimension(Enum):
    """Dimensions of observability coverage."""

    METRICS = "metrics"
    TRACING = "tracing"
    HEALTH = "health"
    LOGGING = "logging"


class ComponentCategory(Enum):
    """Component categories for coverage analysis."""

    SOVEREIGN = "sovereign"
    GATEWAY = "gateway"
    RUNTIME = "runtime"
    TOOL = "tool"
    INFRASTRUCTURE = "infrastructure"


@dataclass
class CoverageCheck:
    """Individual coverage check result."""

    dimension: CoverageDimension
    component: str
    category: ComponentCategory
    check_name: str
    passed: bool
    details: str = ""
    severity: str = "info"  # info, warning, critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "component": self.component,
            "category": self.category.value,
            "check": self.check_name,
            "passed": self.passed,
            "details": self.details,
            "severity": self.severity,
        }


@dataclass
class ComponentCoverage:
    """Coverage summary for a single component."""

    component: str
    category: ComponentCategory
    metrics_count: int = 0
    has_metrics: bool = False
    has_tracing: bool = False
    has_health: bool = False
    has_logging: bool = False
    checks: list[CoverageCheck] = field(default_factory=list)
    score: float = 0.0  # 0-100

    def add_check(self, check: CoverageCheck) -> None:
        self.checks.append(check)

    def compute_score(self) -> float:
        """Compute coverage score (0-100)."""
        if not self.checks:
            self.score = 0.0
            return self.score

        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        self.score = (passed / total) * 100 if total > 0 else 0.0
        return self.score

    def to_dict(self) -> dict[str, Any]:
        self.compute_score()
        return {
            "component": self.component,
            "category": self.category.value,
            "metrics_count": self.metrics_count,
            "has_metrics": self.has_metrics,
            "has_tracing": self.has_tracing,
            "has_health": self.has_health,
            "has_logging": self.has_logging,
            "score": round(self.score, 1),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class CoverageReport:
    """Full observability coverage report."""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    components: dict[str, ComponentCoverage] = field(default_factory=dict)
    overall_score: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)

    def add_component(self, coverage: ComponentCoverage) -> None:
        self.components[coverage.component] = coverage

    def compute_overall(self) -> float:
        """Compute overall coverage score."""
        if not self.components:
            self.overall_score = 0.0
            return self.overall_score

        scores = [c.compute_score() for c in self.components.values()]
        self.overall_score = sum(scores) / len(scores) if scores else 0.0
        return self.overall_score

    def generate_summary(self) -> dict[str, Any]:
        """Generate summary statistics."""
        self.compute_overall()
        total_components = len(self.components)
        categories = {}
        for c in self.components.values():
            cat = c.category.value
            if cat not in categories:
                categories[cat] = {"count": 0, "scores": []}
            categories[cat]["count"] += 1
            categories[cat]["scores"].append(c.score)

        cat_summary = {}
        for cat, data in categories.items():
            cat_summary[cat] = {
                "count": data["count"],
                "avg_score": round(sum(data["scores"]) / len(data["scores"]), 1) if data["scores"] else 0,
            }

        dimensions = {}
        for dim in CoverageDimension:
            passed = 0
            total = 0
            for c in self.components.values():
                for check in c.checks:
                    if check.dimension == dim:
                        total += 1
                        if check.passed:
                            passed += 1
            dimensions[dim.value] = {
                "passed": passed,
                "total": total,
                "coverage": round((passed / total * 100) if total > 0 else 0, 1),
            }

        self.summary = {
            "total_components": total_components,
            "overall_score": round(self.overall_score, 1),
            "by_category": cat_summary,
            "by_dimension": dimensions,
            "gaps": self._identify_gaps(),
        }
        return self.summary

    def _identify_gaps(self) -> list[dict[str, Any]]:
        """Identify coverage gaps."""
        gaps = []
        for comp_name, coverage in self.components.items():
            for check in coverage.checks:
                if not check.passed and check.severity in ("warning", "critical"):
                    gaps.append({
                        "component": comp_name,
                        "category": coverage.category.value,
                        "dimension": check.dimension.value,
                        "check": check.check_name,
                        "severity": check.severity,
                        "details": check.details,
                    })
        # Sort by severity
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        gaps.sort(key=lambda g: severity_order.get(g["severity"], 3))
        return gaps

    def to_dict(self) -> dict[str, Any]:
        self.compute_overall()
        self.generate_summary()
        return {
            "timestamp": self.timestamp,
            "overall_score": round(self.overall_score, 1),
            "summary": self.summary,
            "components": {name: c.to_dict() for name, c in self.components.items()},
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: Path | str) -> None:
        """Save report to file."""
        Path(path).write_text(self.to_json(), encoding="utf-8")


class CoverageAnalyzer:
    """Analyzes observability coverage across components."""

    # Expected metrics per component category
    EXPECTED_METRICS: dict[ComponentCategory, list[str]] = {
        ComponentCategory.SOVEREIGN: [
            "requests_total",
            "request_duration_seconds",
            "decisions_total",
            "delegations_total",
            "errors_total",
        ],
        ComponentCategory.GATEWAY: [
            "requests_total",
            "request_duration_seconds",
            "rate_limited_total",
            "auth_failures_total",
            "contract_denials_total",
            "active_connections",
        ],
        ComponentCategory.RUNTIME: [
            "process_uptime_seconds",
            "memory_usage_bytes",
            "cpu_usage_percent",
            "child_processes",
            "restart_count",
            "health_status",
        ],
        ComponentCategory.TOOL: [
            "invocations_total",
            "invocation_duration_seconds",
            "success_total",
            "failure_total",
            "queue_depth",
        ],
        ComponentCategory.INFRASTRUCTURE: [
            "connections_active",
            "queue_depth",
            "latency_seconds",
            "errors_total",
        ],
    }

    # Required health checks per category
    REQUIRED_HEALTH_CHECKS: dict[ComponentCategory, list[str]] = {
        ComponentCategory.SOVEREIGN: ["governance_ready", "runtime_ready", "dependencies"],
        ComponentCategory.GATEWAY: ["channel_connectivity", "heartbeat_liveness", "generation_validity"],
        ComponentCategory.RUNTIME: ["process_health", "child_health", "resource_availability"],
        ComponentCategory.TOOL: ["service_ready", "channel_health"],
        ComponentCategory.INFRASTRUCTURE: ["connectivity", "resource_availability"],
    }

    def __init__(self, collector: MetricsCollector | None = None) -> None:
        self.collector = collector or get_metrics_collector()
        self._health_checkers: dict[str, HealthChecker] = {}

    def register_health_checker(self, name: str, checker: HealthChecker) -> None:
        self._health_checkers[name] = checker

    def analyze(self) -> CoverageReport:
        """Analyze coverage across all registered components."""
        report = CoverageReport()
        components = self.collector.collect_all()

        # Analyze metrics collector components
        for name, metrics in components.items():
            category = self._infer_category(metrics.component_type)
            coverage = ComponentCoverage(name, category)
            coverage.metrics_count = len(metrics.metrics)

            # Check metrics coverage
            expected = self.EXPECTED_METRICS.get(category, [])
            for exp_metric in expected:
                found = any(exp_metric in m for m in metrics.metrics.keys())
                coverage.add_check(CoverageCheck(
                    dimension=CoverageDimension.METRICS,
                    component=name,
                    category=category,
                    check_name=f"metric_{exp_metric}",
                    passed=found,
                    details=f"Expected metric '{exp_metric}' {'found' if found else 'missing'}",
                    severity="warning" if not found else "info",
                ))

            coverage.has_metrics = coverage.metrics_count > 0

            # Check tracing (correlation ID in metadata)
            coverage.has_tracing = "correlation_id" in metrics.metadata or any(
                "correlation" in m for m in metrics.metrics.keys()
            )
            coverage.add_check(CoverageCheck(
                dimension=CoverageDimension.TRACING,
                component=name,
                category=category,
                check_name="correlation_id_propagation",
                passed=coverage.has_tracing,
                details="Correlation ID propagation implemented" if coverage.has_tracing else "Missing correlation ID propagation",
                severity="warning" if not coverage.has_tracing else "info",
            ))

            report.add_component(coverage)

        # Analyze health checkers
        for name, checker in self._health_checkers.items():
            if name in report.components:
                coverage = report.components[name]
            else:
                category = self._infer_category(checker.component_type)
                coverage = ComponentCoverage(name, category)
                report.add_component(coverage)

            coverage.has_health = True
            expected_checks = self.REQUIRED_HEALTH_CHECKS.get(category, [])
            for exp_check in expected_checks:
                coverage.add_check(CoverageCheck(
                    dimension=CoverageDimension.HEALTH,
                    component=name,
                    category=category,
                    check_name=f"health_{exp_check}",
                    passed=False,  # Would need to run checks to verify
                    details=f"Required health check '{exp_check}' registered",
                    severity="info",
                ))

        return report

    def _infer_category(self, component_type: str) -> ComponentCategory:
        type_lower = component_type.lower()
        if "sovereign" in type_lower:
            return ComponentCategory.SOVEREIGN
        if "gateway" in type_lower:
            return ComponentCategory.GATEWAY
        if "runtime" in type_lower:
            return ComponentCategory.RUNTIME
        if "tool" in type_lower:
            return ComponentCategory.TOOL
        return ComponentCategory.INFRASTRUCTURE


def generate_coverage_report(
    collector: MetricsCollector | None = None,
    health_checkers: dict[str, HealthChecker] | None = None,
    output_path: Path | str | None = None,
) -> CoverageReport:
    """Generate and optionally save a coverage report."""
    analyzer = CoverageAnalyzer(collector)
    if health_checkers:
        for name, checker in health_checkers.items():
            analyzer.register_health_checker(name, checker)
    report = analyzer.analyze()
    if output_path:
        report.save(output_path)
    return report