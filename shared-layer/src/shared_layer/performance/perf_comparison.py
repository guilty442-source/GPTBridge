"""Performance Comparison — comparability verification and baseline comparison.

Before comparing a current measurement against a baseline, the
environment must be verified as comparable.  If the machine, toolchain,
runtime, or config differ, the comparison outputs COMPARISON_INVALID.

Comparison steps:
    1. Verify environment comparability (machine/toolchain/runtime/config)
    2. If not comparable -> COMPARISON_INVALID
    3. If comparable -> compare metrics against tolerance
    4. Correctness/contract/resource safety always overrides performance
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .perf_baseline import EnvironmentProfile, PerformanceBaselineRecord
from .perf_tolerance import (
    ToleranceSet,
    ToleranceCheckResult,
    check_all_tolerances,
)


# Fields that must match for comparability
CRITICAL_ENV_FIELDS = frozenset({
    "python_version",
    "platform",
    "cpu_count",
    "machine",
})

# Fields that affect performance but may vary slightly
SOFT_ENV_FIELDS = frozenset({
    "processor",
    "memory_total_bytes",
    "compiler",
    "numpy_version",
    "pybind11_version",
})


@dataclass(frozen=True)
class ComparabilityResult:
    """Result of environment comparability verification."""
    comparable: bool
    status: str  # "COMPARABLE" or "COMPARISON_INVALID"
    mismatched_fields: tuple[str, ...]
    soft_mismatches: tuple[str, ...]
    message: str


def verify_comparability(
    baseline_env: dict[str, Any] | EnvironmentProfile,
    current_env: dict[str, Any] | EnvironmentProfile,
) -> ComparabilityResult:
    """Verify that two environments are comparable.

    Critical fields (python_version, platform, cpu_count, machine) must
    match exactly.  Soft fields (processor, memory, compiler, numpy,
    pybind11) may differ but are reported as warnings.
    """
    if isinstance(baseline_env, EnvironmentProfile):
        baseline_dict = {k: v for k, v in baseline_env.__dict__.items()}
    else:
        baseline_dict = dict(baseline_env)

    if isinstance(current_env, EnvironmentProfile):
        current_dict = {k: v for k, v in current_env.__dict__.items()}
    else:
        current_dict = dict(current_env)

    mismatched: list[str] = []
    soft_mismatches: list[str] = []

    for field_name in CRITICAL_ENV_FIELDS:
        b_val = baseline_dict.get(field_name)
        c_val = current_dict.get(field_name)
        if b_val != c_val:
            mismatched.append(field_name)

    for field_name in SOFT_ENV_FIELDS:
        b_val = baseline_dict.get(field_name)
        c_val = current_dict.get(field_name)
        if b_val != c_val:
            soft_mismatches.append(field_name)

    if mismatched:
        return ComparabilityResult(
            comparable=False,
            status="COMPARISON_INVALID",
            mismatched_fields=tuple(mismatched),
            soft_mismatches=tuple(soft_mismatches),
            message=(
                f"COMPARISON_INVALID: critical fields mismatch: "
                f"{', '.join(mismatched)}"
            ),
        )

    if soft_mismatches:
        return ComparabilityResult(
            comparable=True,
            status="COMPARABLE",
            mismatched_fields=(),
            soft_mismatches=tuple(soft_mismatches),
            message=(
                f"COMPARABLE with warnings: soft fields differ: "
                f"{', '.join(soft_mismatches)}"
            ),
        )

    return ComparabilityResult(
        comparable=True,
        status="COMPARABLE",
        mismatched_fields=(),
        soft_mismatches=(),
        message="environments are comparable",
    )


@dataclass(frozen=True)
class ComparisonResult:
    """Result of comparing current measurements against a baseline."""
    operation_id: str
    baseline_id: str
    comparability: ComparabilityResult
    status: str  # "PASS" / "WARN" / "FAIL" / "COMPARISON_INVALID"
    metric_results: tuple[ToleranceCheckResult, ...]
    correctness_passed: bool
    regression_metrics: tuple[str, ...]  # metrics that regressed
    summary: str


def compare_against_baseline(
    baseline: PerformanceBaselineRecord,
    current_env: dict[str, Any] | EnvironmentProfile,
    current_metrics: dict[str, float],
    tolerance_set: ToleranceSet,
    *,
    correctness_passed: bool = True,
) -> ComparisonResult:
    """Compare current measurements against a baseline.

    Steps:
        1. Verify environment comparability
        2. If not comparable -> COMPARISON_INVALID
        3. Compare metrics against tolerances
        4. Correctness failure -> FAIL (overrides performance)
    """
    # Step 1: Verify comparability
    comparability = verify_comparability(baseline.environment, current_env)

    if not comparability.comparable:
        return ComparisonResult(
            operation_id=baseline.operation_id,
            baseline_id=baseline.baseline_id,
            comparability=comparability,
            status="COMPARISON_INVALID",
            metric_results=(),
            correctness_passed=correctness_passed,
            regression_metrics=(),
            summary=f"COMPARISON_INVALID: {comparability.message}",
        )

    # Step 2: Check tolerances
    metric_results = check_all_tolerances(tolerance_set, current_metrics)

    # Step 3: Determine overall status
    # Correctness always overrides performance
    if not correctness_passed:
        return ComparisonResult(
            operation_id=baseline.operation_id,
            baseline_id=baseline.baseline_id,
            comparability=comparability,
            status="FAIL",
            metric_results=tuple(metric_results),
            correctness_passed=False,
            regression_metrics=tuple(
                r.metric_name for r in metric_results if r.status != "PASS"
            ),
            summary="FAIL: correctness/contract check failed (overrides performance)",
        )

    # Check metric results
    has_fail = any(r.status == "FAIL" for r in metric_results)
    has_warn = any(r.status == "WARN" for r in metric_results)
    regressed = tuple(r.metric_name for r in metric_results if r.status != "PASS")

    if has_fail:
        status = "FAIL"
        summary = f"FAIL: {len(regressed)} metrics outside fail threshold"
    elif has_warn:
        status = "WARN"
        summary = f"WARN: {len(regressed)} metrics outside warning threshold"
    else:
        status = "PASS"
        summary = "PASS: all metrics within tolerance"

    return ComparisonResult(
        operation_id=baseline.operation_id,
        baseline_id=baseline.baseline_id,
        comparability=comparability,
        status=status,
        metric_results=tuple(metric_results),
        correctness_passed=True,
        regression_metrics=regressed,
        summary=summary,
    )


__all__ = [
    "ComparabilityResult",
    "verify_comparability",
    "ComparisonResult",
    "compare_against_baseline",
    "CRITICAL_ENV_FIELDS",
    "SOFT_ENV_FIELDS",
]
