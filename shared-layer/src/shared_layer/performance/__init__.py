"""Performance Baseline V1 — Python Hotspot & Native Acceleration Benchmark.

Reproducible profile baseline for GPTBridge's main Python execution paths.
Records wall/cpu time, p50/p95/p99, call count, peak memory, allocation,
and hottest callables.  Classifies hotspots and produces versioned baseline
evidence for the existing Native Promotion Gate (A357/A358).

Codex basis:
    A211 — canonical-six-language-role-map (Python=system-control+semantic/
           business logic; C++=profile-proven implementation/performance core).
    A219 — python-api-native-unavailability-fallback (Python fallback always).
    A221 — canonical-native-physical-tree (parser.cpp/vector.cpp/transformer.cpp).
    A357 — native-promotion-gate (PASS/WARN/FAIL based on profile evidence).
    A358 — native-promotion-budget-benchmark-evidence-and-lifecycle
           (end-to-end cost, copy metrics, machine profile, comparability).
"""
from __future__ import annotations

from .profiler import (
    ProfileResult,
    ProfileSampler,
    profile_callable,
    profile_block,
)
from .baseline import (
    BaselineRecord,
    BaselineStore,
    MachineProfile,
    capture_machine_profile,
)
from .classifier import (
    HotspotClassification,
    classify_hotspot,
    BOTTLENECK_CLASSES,
)
from .benchmark import (
    BenchmarkConfig,
    BenchmarkResult,
    BenchmarkSuite,
    SizeClass,
    WarmthClass,
)
from .native_candidate import (
    NativeCandidate,
    PromotionVerdict,
    classify_candidate,
    to_promotion_verdict,
    KEEP_PYTHON,
    OPTIMIZE_PYTHON,
    EXPERIMENTAL_NATIVE,
    NATIVE_CANDIDATE,
)
from .regression import (
    RegressionEvidence,
    compare_baselines,
    detect_regression,
)
from .budgets import (
    PerformanceBudget,
    BudgetCheckResult,
    get_budget,
    list_budgets,
    check_budget,
    check_all_budgets,
)
from .regression_benchmarks import (
    run_regression_benchmarks,
    write_regression_report,
)
from .native_dispatcher import (
    native_available,
    should_dispatch,
    get_threshold,
    DISPATCH_THRESHOLDS,
    token_estimate,
    batch_token_estimate,
    dot,
    l2_norm,
    cosine_similarity,
    matmul,
    softmax,
    scaled_dot_product_attention,
    python_token_estimate,
    python_batch_token_estimate,
    python_dot,
    python_l2_norm,
    python_cosine_similarity,
    python_matmul,
    python_softmax,
    python_scaled_dot_product_attention,
    native_token_estimate,
    native_batch_token_estimate,
    native_dot,
    native_l2_norm,
    native_cosine_similarity,
    native_matmul,
    native_softmax,
    native_scaled_dot_product_attention,
)
from .native_core_benchmark import (
    run_native_core_benchmark,
    write_native_benchmark_report,
    classify_native_result,
)
from .perf_baseline import (
    PERF_BASELINE_VERSION,
    EnvironmentProfile,
    capture_environment_profile,
    WorkloadProfile,
    FullMetrics,
    PerformanceBaselineRecord,
    PerformanceBaselineStore,
)
from .perf_tiers import (
    BenchmarkTier,
    TierConfig,
    TIER_CONFIGS,
    get_tier_config,
    select_tier_by_env,
)
from .perf_tolerance import (
    MetricTolerance,
    ToleranceSet,
    ToleranceCheckResult,
    compute_tolerance,
    compute_tolerance_set,
    check_tolerance,
    check_all_tolerances,
    LOWER_IS_BETTER,
    HIGHER_IS_BETTER,
)
from .perf_comparison import (
    ComparabilityResult,
    ComparisonResult,
    verify_comparability,
    compare_against_baseline,
    CRITICAL_ENV_FIELDS,
    SOFT_ENV_FIELDS,
)
from .perf_trend import (
    MetricTrend,
    RegressionAttribution,
    OperationTrend,
    compute_metric_trend,
    compute_operation_trend,
    attribute_regression,
    METRIC_BOUNDARY_MAP,
)
from .perf_stability import (
    StabilityCheck,
    StabilityResult,
    STABILITY_BUDGETS,
    check_stability_budgets,
    assess_stability,
)
from .perf_regression_control import (
    RegressionControlReport,
    RegressionController,
    write_regression_control_report,
)

__all__ = [
    "ProfileResult",
    "ProfileSampler",
    "profile_callable",
    "profile_block",
    "BaselineRecord",
    "BaselineStore",
    "MachineProfile",
    "capture_machine_profile",
    "HotspotClassification",
    "classify_hotspot",
    "BOTTLENECK_CLASSES",
    "BenchmarkConfig",
    "BenchmarkResult",
    "BenchmarkSuite",
    "SizeClass",
    "WarmthClass",
    "NativeCandidate",
    "PromotionVerdict",
    "classify_candidate",
    "to_promotion_verdict",
    "KEEP_PYTHON",
    "OPTIMIZE_PYTHON",
    "EXPERIMENTAL_NATIVE",
    "NATIVE_CANDIDATE",
    "RegressionEvidence",
    "compare_baselines",
    "detect_regression",
    "PerformanceBudget",
    "BudgetCheckResult",
    "get_budget",
    "list_budgets",
    "check_budget",
    "check_all_budgets",
    "run_regression_benchmarks",
    "write_regression_report",
    "native_available",
    "should_dispatch",
    "get_threshold",
    "DISPATCH_THRESHOLDS",
    "token_estimate",
    "batch_token_estimate",
    "dot",
    "l2_norm",
    "cosine_similarity",
    "matmul",
    "softmax",
    "scaled_dot_product_attention",
    "python_token_estimate",
    "python_batch_token_estimate",
    "python_dot",
    "python_l2_norm",
    "python_cosine_similarity",
    "python_matmul",
    "python_softmax",
    "python_scaled_dot_product_attention",
    "native_token_estimate",
    "native_batch_token_estimate",
    "native_dot",
    "native_l2_norm",
    "native_cosine_similarity",
    "native_matmul",
    "native_softmax",
    "native_scaled_dot_product_attention",
    "run_native_core_benchmark",
    "write_native_benchmark_report",
    "classify_native_result",
    # Performance Budget & Regression Control V1
    "PERF_BASELINE_VERSION",
    "EnvironmentProfile",
    "capture_environment_profile",
    "WorkloadProfile",
    "FullMetrics",
    "PerformanceBaselineRecord",
    "PerformanceBaselineStore",
    "BenchmarkTier",
    "TierConfig",
    "TIER_CONFIGS",
    "get_tier_config",
    "select_tier_by_env",
    "MetricTolerance",
    "ToleranceSet",
    "ToleranceCheckResult",
    "compute_tolerance",
    "compute_tolerance_set",
    "check_tolerance",
    "check_all_tolerances",
    "LOWER_IS_BETTER",
    "HIGHER_IS_BETTER",
    "ComparabilityResult",
    "ComparisonResult",
    "verify_comparability",
    "compare_against_baseline",
    "CRITICAL_ENV_FIELDS",
    "SOFT_ENV_FIELDS",
    "MetricTrend",
    "RegressionAttribution",
    "OperationTrend",
    "compute_metric_trend",
    "compute_operation_trend",
    "attribute_regression",
    "METRIC_BOUNDARY_MAP",
    "StabilityCheck",
    "StabilityResult",
    "STABILITY_BUDGETS",
    "check_stability_budgets",
    "assess_stability",
    "RegressionControlReport",
    "RegressionController",
    "write_regression_control_report",
]
