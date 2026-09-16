"""Shared types for the adaptive SQL control layer.

Every controller in this package is driven by:

  * :class:`LoadSignals` — the observed state of the data plane,
  * :class:`AdaptiveEnvelope` — the pre-approved parameter ranges, and
  * the admission ladder defined in :mod:`admission`.

Adaptive parameters may only move inside the envelope; any caller that
provides values outside it gets them clamped (never an unbounded value).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class PriorityClass(Enum):
    """Traffic classes, ordered by importance (lower rank = more important)."""

    CRITICAL = "critical"
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    MAINTENANCE = "maintenance"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]

    @property
    def default_value(self) -> int:
        return _PRIORITY_VALUE[self]


_PRIORITY_RANK: Final[dict[PriorityClass, int]] = {
    PriorityClass.CRITICAL: 0,
    PriorityClass.INTERACTIVE: 1,
    PriorityClass.BACKGROUND: 2,
    PriorityClass.MAINTENANCE: 3,
}

_PRIORITY_VALUE: Final[dict[PriorityClass, int]] = {
    PriorityClass.CRITICAL: 0,
    PriorityClass.INTERACTIVE: 100,
    PriorityClass.BACKGROUND: 500,
    PriorityClass.MAINTENANCE: 900,
}

PRIORITY_CLASS_ORDER: Final[tuple[PriorityClass, ...]] = (
    PriorityClass.CRITICAL,
    PriorityClass.INTERACTIVE,
    PriorityClass.BACKGROUND,
    PriorityClass.MAINTENANCE,
)


class PressureLevel(Enum):
    """Overall data-plane pressure, derived from :class:`LoadSignals`."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionKind(Enum):
    ALLOW = "allow"
    DEFER = "defer"
    REJECT = "reject"
    BATCH = "batch"
    DEGRADE = "degrade"


@dataclass(frozen=True)
class Decision:
    """Outcome of an admission / cost / budget evaluation."""

    kind: DecisionKind
    reason: str
    retry_after_seconds: float | None = None
    batch_size: int | None = None

    @property
    def allowed(self) -> bool:
        return self.kind in (DecisionKind.ALLOW, DecisionKind.BATCH)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"decision": self.kind.value, "reason": self.reason}
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.batch_size is not None:
            payload["batch_size"] = self.batch_size
        return payload


ALLOW: Final[Decision] = Decision(DecisionKind.ALLOW, "ok")


@dataclass
class LoadSignals:
    """Observed data-plane state used by every adaptive decision.

    All fields are optional so callers can supply whatever their runtime can
    measure; missing values are treated as "unknown / not pressured".
    """

    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    pg_latency_ms: float = 0.0
    pg_wait_ms: float = 0.0
    active_connections: int = 0
    pool_wait_timeouts: int = 0
    lock_contention_pct: float = 0.0
    transport_backlog: int = 0
    reconcile_backlog: int = 0
    qdrant_backlog: int = 0
    qdrant_latency_ms: float = 0.0
    model_load_pct: float = 0.0
    degraded: bool = False
    degraded_seconds: float = 0.0
    sqlite_pending_count: int = 0
    sqlite_db_bytes: int = 0
    sqlite_wal_bytes: int = 0

    def pressure(self, envelope: "AdaptiveEnvelope" | None = None) -> PressureLevel:
        """Coarse pressure level from latency, backlog and system load."""
        limits = envelope or AdaptiveEnvelope()
        score = 0
        if self.pg_latency_ms >= limits.pg_latency_high_ms:
            score += 2
        elif self.pg_latency_ms >= limits.pg_latency_moderate_ms:
            score += 1
        if self.lock_contention_pct >= limits.lock_contention_high_pct:
            score += 2
        elif self.lock_contention_pct >= limits.lock_contention_moderate_pct:
            score += 1
        if self.transport_backlog >= limits.transport_backlog_high:
            score += 2
        elif self.transport_backlog >= limits.transport_backlog_moderate:
            score += 1
        if self.cpu_pct >= limits.cpu_high_pct:
            score += 1
        if self.ram_pct >= limits.ram_high_pct:
            score += 1
        if self.pool_wait_timeouts > 0:
            score += 1
        if score >= 5:
            return PressureLevel.CRITICAL
        if score >= 3:
            return PressureLevel.HIGH
        if score >= 1:
            return PressureLevel.MODERATE
        return PressureLevel.LOW


@dataclass
class ResourceBudget:
    """Per-module ceilings (approved envelope for one tool/module)."""

    module_id: str
    max_connections: int = 4
    max_concurrent_queries: int = 2
    max_pending_requests: int = 1000
    max_write_rate: float = 50.0
    max_reconcile_rate: float = 20.0

    def __post_init__(self) -> None:
        if self.max_connections < 1 or self.max_concurrent_queries < 1:
            raise ValueError("budget limits must be positive")
        if self.max_pending_requests < 0 or self.max_write_rate < 0 or self.max_reconcile_rate < 0:
            raise ValueError("budget rates must be non-negative")


@dataclass
class AdaptiveEnvelope:
    """Pre-approved bounds for every adaptive parameter."""

    pool_min: int = 2
    pool_max: int = 8
    batch_min: int = 50
    batch_max: int = 500
    reconcile_workers_min: int = 1
    reconcile_workers_max: int = 2
    qdrant_upserts_min_per_second: float = 10.0
    qdrant_upserts_max_per_second: float = 200.0
    max_rows_batch: int = 5_000
    max_rows_background: int = 50_000
    max_rows_hard_reject: int = 500_000
    transport_backlog_moderate: int = 200
    transport_backlog_high: int = 1000
    pg_latency_moderate_ms: float = 80.0
    pg_latency_high_ms: float = 250.0
    lock_contention_moderate_pct: float = 5.0
    lock_contention_high_pct: float = 20.0
    cpu_high_pct: float = 85.0
    ram_high_pct: float = 90.0
    min_dwell_seconds: float = 30.0
    max_pool_step: int = 1
    max_batch_step: int = 50

    def clamp_pool(self, value: int) -> int:
        return max(self.pool_min, min(self.pool_max, int(value)))

    def clamp_batch(self, value: int) -> int:
        return max(self.batch_min, min(self.batch_max, int(value)))

    def clamp_reconcile_workers(self, value: int) -> int:
        return max(
            self.reconcile_workers_min,
            min(self.reconcile_workers_max, int(value)),
        )

    def clamp_upsert_rate(self, value: float) -> float:
        return max(
            self.qdrant_upserts_min_per_second,
            min(self.qdrant_upserts_max_per_second, float(value)),
        )


DEFAULT_ENVELOPE: Final[AdaptiveEnvelope] = AdaptiveEnvelope()
