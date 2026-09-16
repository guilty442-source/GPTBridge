"""Cross-language end-to-end performance framework (V1).

Reusable layer: identity-carrying spans/traces, real stage boundaries,
critical-path analysis (interval union — never naive async summation),
benchmark runner (cold/warm/contended, p50/p95/p99), governed
classification, before/after native comparison.
"""
from .benchmark import E2EBenchmark, PathBenchmark
from .classify import (
    NativeVerdict,
    PathClass,
    classify_path,
    compare_native,
    native_share,
)
from .critical_path import CriticalPathReport, PhaseProfile, aggregate, analyze
from .paths import PATHS, PathContext
from .harness import E2EHarness
from .spans import (
    Phase,
    RequestTrace,
    Span,
    TraceCollector,
    TraceContext,
)

__all__ = [
    "E2EBenchmark",
    "E2EHarness",
    "PathBenchmark",
    "PathContext",
    "PATHS",
    "Phase",
    "RequestTrace",
    "Span",
    "TraceCollector",
    "TraceContext",
    "PhaseProfile",
    "CriticalPathReport",
    "analyze",
    "aggregate",
    "PathClass",
    "NativeVerdict",
    "classify_path",
    "compare_native",
    "native_share",
]
