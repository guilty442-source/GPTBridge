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
]
