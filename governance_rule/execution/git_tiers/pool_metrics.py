"""Worker pool queue metrics and health classification (task §63).

Metrics the supervisor uses to judge pool pressure:

    active_workers, free_workers, quarantined_workers, waiting_tasks,
    merge_queue_depth, oldest_queue_age, average_merge_latency,
    commit_rate, conflict_rate, worker_retire_rate, allocation_latency

Classification: HEALTHY / BUSY / BACKPRESSURE / DEGRADED / ERROR.
Scores are monitoring summaries only — never governance authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MetricHealth(Enum):
    HEALTHY = "HEALTHY"
    BUSY = "BUSY"
    BACKPRESSURE = "BACKPRESSURE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PoolMetrics:
    active_workers: int = 0
    free_workers: int = 0
    quarantined_workers: int = 0
    waiting_tasks: int = 0
    merge_queue_depth: int = 0
    oldest_queue_age_seconds: float = 0.0
    average_merge_latency_seconds: float = 0.0
    commit_rate_per_hour: float = 0.0
    conflict_rate: float = 0.0          # conflicts per merge attempt
    worker_retire_rate_per_hour: float = 0.0
    allocation_latency_seconds: float = 0.0


@dataclass(frozen=True)
class MetricThresholds:
    busy_queue_depth: int = 10
    backpressure_queue_depth: int = 50
    degraded_quarantined: int = 1
    degraded_oldest_queue_age: float = 3600.0
    degraded_conflict_rate: float = 0.30


def classify_pool_health(
    metrics: PoolMetrics,
    thresholds: Optional[MetricThresholds] = None,
    *,
    backpressure_flag: bool = False,
    pool_error: bool = False,
) -> MetricHealth:
    """Classify pool health from observed metrics.

    ERROR only when the pool itself failed (registry corrupt, git
    unreachable) — degraded inputs classify DEGRADED, not ERROR.
    """
    thresholds = thresholds or MetricThresholds()
    if pool_error:
        return MetricHealth.ERROR
    if backpressure_flag or (
        metrics.merge_queue_depth >= thresholds.backpressure_queue_depth
    ):
        return MetricHealth.BACKPRESSURE
    if (
        metrics.quarantined_workers >= thresholds.degraded_quarantined
        or metrics.oldest_queue_age_seconds
        >= thresholds.degraded_oldest_queue_age
        or metrics.conflict_rate >= thresholds.degraded_conflict_rate
    ):
        return MetricHealth.DEGRADED
    if metrics.merge_queue_depth >= thresholds.busy_queue_depth:
        return MetricHealth.BUSY
    return MetricHealth.HEALTHY


__all__ = [
    "MetricHealth",
    "MetricThresholds",
    "PoolMetrics",
    "classify_pool_health",
]
