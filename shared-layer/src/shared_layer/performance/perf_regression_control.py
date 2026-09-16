"""Performance Regression Control — main orchestrator.

Coordinates the full regression control pipeline:
    1. Select benchmark tier (PERF_SMOKE / PERF_STANDARD / PERF_FULL)
    2. Run benchmarks at the selected tier
    3. Verify environment comparability against baseline
    4. Compare current metrics against baseline with statistical tolerance
    5. Compute historical trend and regression attribution
    6. Assess performance stability (PERFORMANCE_STABLE marking)
    7. Produce a regression control report

Baseline update policy:
    Baseline updates must be explicit accepted intentional changes.
    The system NEVER auto-resets a baseline to eliminate a regression.
    An intentional update requires:
        - intentional_update=True
        - update_reason (non-empty string)
    The update is recorded but does not erase the previous baseline;
    the history is preserved for trend analysis.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .perf_baseline import (
    PerformanceBaselineRecord,
    PerformanceBaselineStore,
    EnvironmentProfile,
    capture_environment_profile,
    now_iso,
    PERF_BASELINE_VERSION,
)
from .perf_tiers import BenchmarkTier, get_tier_config, select_tier_by_env
from .perf_tolerance import (
    ToleranceSet,
    compute_tolerance_set,
    check_all_tolerances,
)
from .perf_comparison import (
    ComparabilityResult,
    ComparisonResult,
    verify_comparability,
    compare_against_baseline,
)
from .perf_trend import (
    OperationTrend,
    compute_operation_trend,
    RegressionAttribution,
)
from .perf_stability import (
    StabilityResult,
    assess_stability,
)


@dataclass(frozen=True)
class RegressionControlReport:
    """Full regression control report for one benchmark run."""
    report_id: str
    recorded_at: str
    tier: str
    operation_id: str
    baseline_id: str
    comparability_status: str
    comparison_status: str
    stability_status: str
    trend_direction: str
    correctness_passed: bool
    metric_results: list[dict[str, Any]]
    regressions: list[dict[str, Any]]
    stability_checks: list[dict[str, Any]]
    summary: str


class RegressionController:
    """Main regression control orchestrator.

    Usage:
        controller = RegressionController(baseline_store)
        report = controller.run(
            operation_id="parser.token_estimate",
            current_env=env,
            current_metrics=metrics,
            correctness_passed=True,
        )
    """

    def __init__(self, baseline_store: PerformanceBaselineStore) -> None:
        self.store = baseline_store

    def run(
        self,
        operation_id: str,
        current_env: EnvironmentProfile | dict[str, Any],
        current_metrics: dict[str, float],
        *,
        correctness_passed: bool = True,
        tier: BenchmarkTier | None = None,
        tolerance_set: ToleranceSet | None = None,
    ) -> RegressionControlReport:
        """Run regression control for one operation.

        Steps:
            1. Get the latest baseline for this operation
            2. Verify environment comparability
            3. Compare metrics against baseline
            4. Compute trend
            5. Assess stability
            6. Produce report
        """
        if tier is None:
            tier = select_tier_by_env()

        # Step 1: Get latest baseline
        baseline = self.store.latest(operation_id)

        if baseline is None:
            # No baseline yet; record as first baseline
            return self._record_first_baseline(
                operation_id, current_env, current_metrics,
                correctness_passed, tier,
            )

        # Step 2: Verify comparability
        comparability = verify_comparability(baseline.environment, current_env)

        # Step 3: Compare against baseline
        if tolerance_set is None:
            # Build tolerance from history
            tolerance_set = self._build_tolerance_from_history(operation_id)

        comparison = compare_against_baseline(
            baseline, current_env, current_metrics, tolerance_set,
            correctness_passed=correctness_passed,
        )

        # Step 4: Compute trend
        history = self.store.history(operation_id)
        trend = compute_operation_trend(operation_id, history)

        # Step 5: Assess stability
        stability = assess_stability(
            baseline, trend,
            correctness_passed=correctness_passed,
        )

        # Step 6: Produce report
        metric_results = [
            {
                "metric": r.metric_name,
                "actual": r.actual,
                "mean": r.mean,
                "std_dev": r.std_dev,
                "warning_threshold": r.warning_threshold,
                "fail_threshold": r.fail_threshold,
                "status": r.status,
                "delta_pct": r.delta_pct,
                "message": r.message,
            }
            for r in comparison.metric_results
        ]

        regressions = [
            {
                "operation_id": r.operation_id,
                "metric_name": r.metric_name,
                "baseline_value": r.baseline_value,
                "current_value": r.current_value,
                "delta_pct": r.delta_pct,
                "status": r.status,
                "dominant_boundary": r.dominant_boundary,
                "dominant_span": r.dominant_span,
                "message": r.message,
            }
            for r in trend.regressions
        ]

        stability_checks = [
            {
                "metric_name": c.metric_name,
                "actual": c.actual,
                "budget_max": c.budget_max,
                "passed": c.passed,
                "message": c.message,
            }
            for c in stability.checks
        ]

        return RegressionControlReport(
            report_id=f"regression-control-{operation_id}-{now_iso()}",
            recorded_at=now_iso(),
            tier=tier.value,
            operation_id=operation_id,
            baseline_id=baseline.baseline_id,
            comparability_status=comparability.status,
            comparison_status=comparison.status,
            stability_status=stability.stability,
            trend_direction=trend.overall_direction,
            correctness_passed=correctness_passed,
            metric_results=metric_results,
            regressions=regressions,
            stability_checks=stability_checks,
            summary=self._build_summary(
                comparison, stability, trend, correctness_passed
            ),
        )

    def record_intentional_baseline_update(
        self,
        operation_id: str,
        env: EnvironmentProfile | dict[str, Any],
        workload: dict[str, Any],
        metrics: dict[str, Any],
        *,
        revision: str = "",
        hotspot_class: str = "unknown",
        verdict: str = "UNKNOWN",
        update_reason: str,
        notes: str = "",
    ) -> str:
        """Record an intentional baseline update.

        This is the ONLY way to update a baseline.  The system never
        auto-resets a baseline to eliminate a regression.  The update
        requires an explicit reason and is marked intentional_update=True.
        The previous baseline is preserved in history.
        """
        if not update_reason or not update_reason.strip():
            raise ValueError("intentional baseline update requires a reason")

        baseline_id = self.store.next_id(operation_id)

        if isinstance(env, EnvironmentProfile):
            env_dict = {k: v for k, v in env.__dict__.items()}
        else:
            env_dict = dict(env)

        record = PerformanceBaselineRecord(
            baseline_id=baseline_id,
            baseline_version=PERF_BASELINE_VERSION,
            operation_id=operation_id,
            revision=revision,
            recorded_at=now_iso(),
            environment=env_dict,
            workload=workload,
            metrics=metrics,
            hotspot_class=hotspot_class,
            verdict=verdict,
            stability="UNKNOWN",  # will be assessed later
            notes=notes,
            intentional_update=True,
            update_reason=update_reason,
        )

        return self.store.record(record)

    def _record_first_baseline(
        self,
        operation_id: str,
        env: EnvironmentProfile | dict[str, Any],
        metrics: dict[str, float],
        correctness_passed: bool,
        tier: BenchmarkTier,
    ) -> RegressionControlReport:
        """Record the first baseline for an operation."""
        baseline_id = self.store.next_id(operation_id)

        if isinstance(env, EnvironmentProfile):
            env_dict = {k: v for k, v in env.__dict__.items()}
        else:
            env_dict = dict(env)

        record = PerformanceBaselineRecord(
            baseline_id=baseline_id,
            baseline_version=PERF_BASELINE_VERSION,
            operation_id=operation_id,
            revision="",
            recorded_at=now_iso(),
            environment=env_dict,
            workload={"tier": tier.value, "size": "medium", "warmth": "warm", "concurrency": 1},
            metrics=metrics,
            hotspot_class="unknown",
            verdict="UNKNOWN",
            stability="UNKNOWN",
            notes="first baseline (auto-recorded)",
            intentional_update=False,
            update_reason="",
        )

        self.store.record(record)

        return RegressionControlReport(
            report_id=f"regression-control-{operation_id}-{now_iso()}",
            recorded_at=now_iso(),
            tier=tier.value,
            operation_id=operation_id,
            baseline_id=baseline_id,
            comparability_status="COMPARABLE",
            comparison_status="PASS",
            stability_status="UNKNOWN",
            trend_direction="unknown",
            correctness_passed=correctness_passed,
            metric_results=[],
            regressions=[],
            stability_checks=[],
            summary=f"first baseline recorded: {baseline_id}",
        )

    def _build_tolerance_from_history(self, operation_id: str) -> ToleranceSet:
        """Build a tolerance set from baseline history."""
        history = self.store.history(operation_id)

        if len(history) < 2:
            # Not enough history for statistical tolerance
            # Return a minimal tolerance set
            return ToleranceSet(
                operation_id=operation_id,
                tolerances={},
                stable_run_count=0,
                method="fallback",
            )

        # Collect metric values from history
        metric_runs: dict[str, list[float]] = {}
        for record in history:
            for metric_name, value in record.metrics.items():
                if metric_name not in metric_runs:
                    metric_runs[metric_name] = []
                try:
                    metric_runs[metric_name].append(float(value))
                except (TypeError, ValueError):
                    pass

        return compute_tolerance_set(operation_id, metric_runs)

    def _build_summary(
        self,
        comparison: ComparisonResult,
        stability: StabilityResult,
        trend: OperationTrend,
        correctness_passed: bool,
    ) -> str:
        parts: list[str] = []

        if not correctness_passed:
            parts.append("FAIL: correctness/contract check failed")
        else:
            parts.append(f"comparison={comparison.status}")
            parts.append(f"stability={stability.stability}")
            parts.append(f"trend={trend.overall_direction}")

            if comparison.regression_metrics:
                parts.append(f"regressed_metrics={','.join(comparison.regression_metrics)}")

            if stability.stability == "PERFORMANCE_STABLE":
                parts.append("stop evidence-free micro-optimization")

        return "; ".join(parts)


def write_regression_control_report(
    report: RegressionControlReport,
    path: Path,
) -> None:
    """Write a regression control report to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


__all__ = [
    "RegressionControlReport",
    "RegressionController",
    "write_regression_control_report",
]
