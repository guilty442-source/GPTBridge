"""Capacity Watermark Manager (A369 + A8/E21).

Assigns every storage target a four-level watermark:

    normal       — within budget
    warning      — approaching budget; proposals start
    critical     — budget exhausted soon; non-essential work defers
    fail-closed  — writes denied; only governance/critical transport pass

Monitored targets (one entry each):
    pg_data, pg_wal, sqlite, sqlite_wal, qdrant, backup, archive

The layer only *classifies* and *proposes*.  It never deletes,
purges, or mutates storage — those actions stay governed (A366
confirmation, A367 stage order, A369 controls).

Usage:
    from shared_layer.database.capacity_watermark import (
        WatermarkPolicy, evaluate_target, evaluate_all,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WatermarkLevel(Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    FAIL_CLOSED = "fail-closed"


#: Store targets monitored by the watermark manager.  Keys are stable
#: semantic codes (A386) — physical paths resolve elsewhere.
WATERMARK_TARGETS: tuple[str, ...] = (
    "pg_data",
    "pg_wal",
    "sqlite",
    "sqlite_wal",
    "qdrant",
    "backup",
    "archive",
)


@dataclass(frozen=True)
class WatermarkPolicy:
    """Fraction-of-capacity thresholds for one target.

    ``warning``/``critical``/``fail_closed`` are fractions of the
    target's provisioned capacity (0..1).  ``fail_closed`` marks the
    point where writes for that target are denied outright.
    """

    target: str
    warning: float = 0.70
    critical: float = 0.85
    fail_closed: float = 0.95

    def __post_init__(self) -> None:
        if not 0.0 < self.warning < self.critical < self.fail_closed <= 1.0:
            raise ValueError(
                f"watermark thresholds for {self.target} must satisfy "
                "0 < warning < critical < fail_closed <= 1"
            )


@dataclass(frozen=True)
class TargetStatus:
    """Evaluated watermark state for one target."""

    target: str
    used_bytes: int
    capacity_bytes: int
    ratio: float
    level: WatermarkLevel


@dataclass(frozen=True)
class WatermarkReport:
    """Aggregate watermark state across all targets."""

    statuses: tuple[TargetStatus, ...]
    worst: WatermarkLevel

    @property
    def fail_closed_targets(self) -> tuple[str, ...]:
        return tuple(
            s.target for s in self.statuses
            if s.level is WatermarkLevel.FAIL_CLOSED
        )


_DEFAULT_POLICIES: dict[str, WatermarkPolicy] = {
    t: WatermarkPolicy(target=t) for t in WATERMARK_TARGETS
}


def evaluate_target(
    target: str,
    used_bytes: int,
    capacity_bytes: int,
    policy: Optional[WatermarkPolicy] = None,
) -> TargetStatus:
    """Classify one target's fill ratio into a watermark level."""
    policy = policy or _DEFAULT_POLICIES.get(
        target, WatermarkPolicy(target=target)
    )
    if capacity_bytes <= 0:
        # Unknown capacity cannot prove headroom — fail closed.
        return TargetStatus(target, used_bytes, capacity_bytes, 1.0,
                            WatermarkLevel.FAIL_CLOSED)
    ratio = used_bytes / capacity_bytes
    if ratio >= policy.fail_closed:
        level = WatermarkLevel.FAIL_CLOSED
    elif ratio >= policy.critical:
        level = WatermarkLevel.CRITICAL
    elif ratio >= policy.warning:
        level = WatermarkLevel.WARNING
    else:
        level = WatermarkLevel.NORMAL
    return TargetStatus(target, used_bytes, capacity_bytes, ratio, level)


def evaluate_all(
    usage: dict[str, tuple[int, int]],
    policies: Optional[dict[str, WatermarkPolicy]] = None,
) -> WatermarkReport:
    """Evaluate ``{target: (used_bytes, capacity_bytes)}`` for every
    declared target.  Missing targets evaluate as capacity-unknown
    (fail-closed) — absent measurements cannot prove headroom."""
    statuses = []
    worst = WatermarkLevel.NORMAL
    order = list(WatermarkLevel)
    for target in WATERMARK_TARGETS:
        used, capacity = usage.get(target, (0, 0))
        policy = (policies or {}).get(target)
        status = evaluate_target(target, used, capacity, policy)
        statuses.append(status)
        if order.index(status.level) > order.index(worst):
            worst = status.level
    return WatermarkReport(tuple(statuses), worst)


__all__ = [
    "WATERMARK_TARGETS",
    "WatermarkLevel",
    "WatermarkPolicy",
    "WatermarkReport",
    "TargetStatus",
    "evaluate_all",
    "evaluate_target",
]
