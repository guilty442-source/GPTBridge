"""Performance Baseline V2 — versioned cross-language end-to-end baseline.

Extends the V1 BaselineRecord with the full metric set required by
Performance Budget & Regression Control V1:

    - revision, machine/workload profile
    - p50/p95/p99, throughput, CPU
    - peak/retained memory
    - SQL query/row/byte count
    - TypeScript<->Python / Python<->native / Python<->C# boundary crossings
    - serialization bytes
    - native copy/allocation
    - queue/model wait

This is a file-based versioned baseline store, not a SQL gate.  It does
not block commits or add audit checks.  Baseline updates must be
explicit accepted intentional changes (see perf_regression_control.py).
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import process_metrics as _metrics

PERF_BASELINE_VERSION = "2.0"


@dataclass(frozen=True)
class EnvironmentProfile:
    """Full environment profile for comparability verification.

    Two baselines are comparable only if their EnvironmentProfiles match
    on the critical fields (python_version, platform, cpu_count, and
    toolchain).  If they don't, the comparison outputs COMPARISON_INVALID.
    """
    python_version: str
    platform: str
    processor: str
    cpu_count: int
    memory_total_bytes: int
    machine: str
    # Toolchain / runtime / config that affect performance
    compiler: str = ""        # e.g. "MSVC 14.51" for native
    numpy_version: str = ""  # numpy affects vector/transformer paths
    pybind11_version: str = ""
    config_hash: str = ""    # hash of relevant config (env vars, etc.)
    baseline_tool_version: str = PERF_BASELINE_VERSION


def capture_environment_profile() -> EnvironmentProfile:
    """Capture the current environment profile."""
    cpu_count = _metrics.cpu_count()
    mem = max(0, _metrics.system_memory_total_bytes())

    # Detect compiler (best-effort)
    compiler = ""
    try:
        import subprocess
        r = subprocess.run(
            ["cl", "/?"], capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0 and r.stdout:
            first_line = r.stdout.splitlines()[0]
            compiler = first_line.strip()
    except Exception:
        pass

    # Detect numpy version
    numpy_version = ""
    try:
        import numpy
        numpy_version = numpy.__version__
    except ImportError:
        pass

    # Detect pybind11 version
    pybind11_version = ""
    try:
        import pybind11
        pybind11_version = pybind11.__version__
    except ImportError:
        pass

    return EnvironmentProfile(
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        processor=platform.processor() or "unknown",
        cpu_count=cpu_count,
        memory_total_bytes=mem,
        machine=platform.machine(),
        compiler=compiler,
        numpy_version=numpy_version,
        pybind11_version=pybind11_version,
    )


@dataclass(frozen=True)
class WorkloadProfile:
    """Workload profile for one benchmark configuration."""
    size_class: str       # "small" / "medium" / "large"
    warmth: str           # "cold" / "warm"
    concurrency: int      # 1 / 4 / 8
    input_size: int       # e.g. token count, vector dim, matrix rows
    sample_count: int     # how many samples were collected
    warmup_count: int     # how many warmup runs


@dataclass(frozen=True)
class FullMetrics:
    """Full metric set for one benchmark run.

    All metrics are per-invocation unless noted.
    """
    # Latency (seconds)
    wall_p50: float
    wall_p95: float
    wall_p99: float
    cpu_p50: float
    cpu_p95: float
    cpu_p99: float
    # Throughput (ops/sec)
    throughput_p50: float
    # Memory (bytes)
    peak_memory_p50: float
    retained_memory_p50: float  # memory still held after GC
    # SQL
    sql_query_count: int
    sql_row_count: int
    sql_byte_count: int
    # Boundary crossings
    ts_python_crossings: int    # TypeScript<->Python
    python_native_crossings: int # Python<->native (pybind11/C ABI)
    python_csharp_crossings: int # Python<->C#
    # Serialization
    serialization_bytes: int
    serialization_count: int
    # Native copy/allocation
    native_copy_bytes: int
    native_allocation_count: int
    # Queue/model wait
    queue_wait_ms: float
    model_wait_ms: float
    # Call count
    call_count: int
    # Allocation count (tracemalloc)
    allocation_count: int


@dataclass(frozen=True)
class PerformanceBaselineRecord:
    """One versioned performance baseline record.

    A baseline is a stable reference point.  Updates must be explicit
    accepted intentional changes, not auto-reset to eliminate regression.
    """
    baseline_id: str           # e.g. "parser.token_estimate-v0001"
    baseline_version: str      # "2.0"
    operation_id: str          # e.g. "parser.token_estimate"
    revision: str             # git revision hash
    recorded_at: str           # ISO timestamp
    environment: dict[str, Any]   # EnvironmentProfile as dict
    workload: dict[str, Any]      # WorkloadProfile as dict
    metrics: dict[str, Any]       # FullMetrics as dict
    hotspot_class: str           # cpu_bound / memory_copy_bound / etc.
    verdict: str                 # NATIVE_CANDIDATE / KEEP_PYTHON / etc.
    stability: str               # PERFORMANCE_STABLE / UNSTABLE / UNKNOWN
    notes: str = ""
    # Whether this baseline was an explicit accepted intentional update
    intentional_update: bool = False
    # Reason for update (if intentional)
    update_reason: str = ""


class PerformanceBaselineStore:
    """File-based versioned performance baseline store.

    Records are appended to a JSON file; each record has a monotonic
    ``baseline_id`` so regressions can be compared across versions.
    Baseline updates require explicit acceptance (intentional_update=True).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8") or "[]")

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def record(self, record: PerformanceBaselineRecord) -> str:
        """Append a baseline record; returns the baseline_id."""
        records = self._load()
        records.append(asdict(record))
        self._save(records)
        return record.baseline_id

    def latest(self, operation_id: str) -> PerformanceBaselineRecord | None:
        """Get the most recent baseline for an operation."""
        records = self._load()
        matching = [r for r in records if r["operation_id"] == operation_id]
        if not matching:
            return None
        return self._dict_to_record(matching[-1])

    def latest_intentional(self, operation_id: str) -> PerformanceBaselineRecord | None:
        """Get the most recent intentional baseline for an operation."""
        records = self._load()
        matching = [
            r for r in records
            if r["operation_id"] == operation_id and r.get("intentional_update", False)
        ]
        if not matching:
            return None
        return self._dict_to_record(matching[-1])

    def history(self, operation_id: str) -> list[PerformanceBaselineRecord]:
        """Get all baselines for an operation, oldest first."""
        records = self._load()
        matching = [r for r in records if r["operation_id"] == operation_id]
        return [self._dict_to_record(r) for r in matching]

    def all_operations(self) -> list[str]:
        """Get all operation IDs that have baselines."""
        records = self._load()
        return sorted(set(r["operation_id"] for r in records))

    def next_id(self, operation_id: str) -> str:
        """Generate the next monotonic baseline_id for an operation."""
        history = self.history(operation_id)
        seq = len(history) + 1
        return f"{operation_id}-v{seq:04d}"

    def _dict_to_record(self, d: dict[str, Any]) -> PerformanceBaselineRecord:
        return PerformanceBaselineRecord(
            baseline_id=d["baseline_id"],
            baseline_version=d["baseline_version"],
            operation_id=d["operation_id"],
            revision=d.get("revision", ""),
            recorded_at=d["recorded_at"],
            environment=d.get("environment", {}),
            workload=d.get("workload", {}),
            metrics=d.get("metrics", {}),
            hotspot_class=d.get("hotspot_class", "unknown"),
            verdict=d.get("verdict", "UNKNOWN"),
            stability=d.get("stability", "UNKNOWN"),
            notes=d.get("notes", ""),
            intentional_update=d.get("intentional_update", False),
            update_reason=d.get("update_reason", ""),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PERF_BASELINE_VERSION",
    "EnvironmentProfile",
    "capture_environment_profile",
    "WorkloadProfile",
    "FullMetrics",
    "PerformanceBaselineRecord",
    "PerformanceBaselineStore",
    "now_iso",
]
