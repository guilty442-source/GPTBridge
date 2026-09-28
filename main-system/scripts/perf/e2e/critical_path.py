"""Critical-path analysis over a RequestTrace.

Async spans must NOT be summed — overlapping spans would double-count.  The
critical path is computed by mapping every span's [start, end) interval onto
the request wall-clock and taking the union coverage per phase.  A phase's
critical share is the wall-clock time during which that phase was actively
on the longest serial chain (its union coverage divided by the request's
total wall time).

For strictly sequential paths this degenerates to per-phase sums; for paths
with parallel children (shadow queries, batch workers) it stays honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .spans import Phase, RequestTrace


def _union_ns(intervals: list[tuple[int, int]]) -> int:
    """Total wall-clock covered by the union of [start, end) intervals."""
    if not intervals:
        return 0
    intervals = sorted(intervals)
    total = 0
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)


@dataclass(frozen=True)
class PhaseProfile:
    phase: str
    wall_ns: int            # union coverage on the request wall-clock
    exec_ns: int            # summed execution (queue excluded)
    queue_ns: int           # summed queue wait
    share: float            # wall_ns / total_ns
    span_count: int
    boundary_crossings: int = 0
    sql_roundtrips: int = 0
    bytes: int = 0
    native_copies: int = 0
    native_allocs: int = 0


@dataclass(frozen=True)
class CriticalPathReport:
    path_name: str
    total_ns: int
    phases: tuple[PhaseProfile, ...]
    dominant_phase: str
    boundary_crossings: int
    sql_roundtrips: int
    serialization_bytes: int
    native_copies: int
    native_allocs: int
    queue_wait_ns: int
    execution_ns: int


def analyze(trace: RequestTrace) -> CriticalPathReport:
    by_phase: dict[str, dict[str, Any]] = {}
    for span in trace.spans:
        entry = by_phase.setdefault(
            span.phase.value,
            {"intervals": [], "exec": 0, "queue": 0, "bytes": 0,
             "boundary": 0, "sql": 0, "copies": 0, "allocs": 0, "count": 0},
        )
        entry["intervals"].append((span.start_ns, span.end_ns))
        entry["exec"] += span.exec_ns or span.duration_ns - span.queue_ns
        entry["queue"] += span.queue_ns
        entry["bytes"] += span.bytes
        entry["boundary"] += span.boundary_crossings
        entry["sql"] += span.sql_roundtrips
        entry["copies"] += span.native_copies
        entry["allocs"] += span.native_allocs
        entry["count"] += 1

    total = max(1, trace.total_ns)
    profiles: list[PhaseProfile] = []
    for phase, entry in by_phase.items():
        wall = _union_ns(entry["intervals"])
        profiles.append(
            PhaseProfile(
                phase=phase,
                wall_ns=wall,
                exec_ns=entry["exec"],
                queue_ns=entry["queue"],
                share=wall / total,
                span_count=entry["count"],
                boundary_crossings=entry["boundary"],
                sql_roundtrips=entry["sql"],
                bytes=entry["bytes"],
                native_copies=entry["copies"],
                native_allocs=entry["allocs"],
            )
        )
    profiles.sort(key=lambda p: -p.wall_ns)

    all_intervals = [
        (span.start_ns, span.end_ns) for span in trace.spans
    ]
    covered = _union_ns(all_intervals)
    return CriticalPathReport(
        path_name=trace.path_name,
        total_ns=trace.total_ns,
        phases=tuple(profiles),
        dominant_phase=profiles[0].phase if profiles else "none",
        boundary_crossings=sum(p.boundary_crossings for p in profiles),
        sql_roundtrips=sum(p.sql_roundtrips for p in profiles),
        serialization_bytes=sum(p.bytes for p in profiles),
        native_copies=sum(p.native_copies for p in profiles),
        native_allocs=sum(p.native_allocs for p in profiles),
        queue_wait_ns=sum(p.queue_ns for p in profiles),
        execution_ns=sum(p.exec_ns for p in profiles),
    )


def aggregate(reports: list[CriticalPathReport]) -> dict[str, Any]:
    """Mean per-phase share across runs of one path (for classification)."""
    if not reports:
        return {}
    phases: dict[str, list[float]] = {}
    for report in reports:
        for profile in report.phases:
            phases.setdefault(profile.phase, []).append(profile.share)
    return {
        phase: sum(shares) / len(shares)
        for phase, shares in phases.items()
    }


__all__ = ["PhaseProfile", "CriticalPathReport", "analyze", "aggregate"]
