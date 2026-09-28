"""Cross-language end-to-end performance framework (V1).

Reusable layer: identity-carrying spans/traces, real stage boundaries,
critical-path analysis (interval union — never naive async summation),
benchmark runner (cold/warm/contended, p50/p95/p99), governed
classification, before/after native comparison.

Relocated to ``main-system/scripts/perf`` (dev-verification zone): the
harness wires ``core_system.native`` + ``shared_layer`` internals, so the
repo's source roots are bootstrapped onto ``sys.path`` here.
"""
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[4]
for _p in (
    _ROOT / "main-system" / "src-core",
    _ROOT / "shared-layer" / "src",
    _ROOT / "governance_rule",
):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

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
