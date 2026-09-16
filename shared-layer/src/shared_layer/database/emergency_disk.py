"""Emergency Disk Policy (A369 emergency controls).

When free disk drops through the declared thresholds, capability is
shed in a fixed order — cheapest-to-lose first, critical transport
last:

    warning   -> stop background indexing, stop Qdrant reindex
    critical  -> + stop reconcile, stop optional writes,
                 reduce audit detail to critical events
    fail-closed -> + read-only for non-critical writers;
                 critical transport and governance audit keep
                 writing (evidence must survive the emergency)

Every shed is reversible, visible and audited.  The policy never
purges data, never drops indexes, never reopens a failed store —
those stay behind normal governance even in an emergency (A369).

Usage:
    from shared_layer.database.emergency_disk import (
        EmergencyDiskPolicy, evaluate_disk, CapabilitySet,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EmergencyLevel(Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    FAIL_CLOSED = "fail-closed"


@dataclass(frozen=True)
class EmergencyDiskPolicy:
    warning_free_fraction: float = 0.15
    critical_free_fraction: float = 0.08
    fail_closed_free_fraction: float = 0.03

    def __post_init__(self) -> None:
        if not (
            0.0
            < self.fail_closed_free_fraction
            < self.critical_free_fraction
            < self.warning_free_fraction
            < 1.0
        ):
            raise ValueError(
                "0 < fail_closed < critical < warning < 1 required"
            )


@dataclass(frozen=True)
class CapabilitySet:
    """What each level permits.  Everything defaults to shed = False;
    levels progressively set capabilities to True (shed)."""

    background_indexing_shed: bool = False
    qdrant_reindex_shed: bool = False
    reconcile_shed: bool = False
    optional_writes_shed: bool = False
    audit_detail_reduced: bool = False
    read_only_noncritical: bool = False


_LEVEL_SHED = {
    EmergencyLevel.NORMAL: CapabilitySet(),
    EmergencyLevel.WARNING: CapabilitySet(
        background_indexing_shed=True, qdrant_reindex_shed=True,
    ),
    EmergencyLevel.CRITICAL: CapabilitySet(
        background_indexing_shed=True, qdrant_reindex_shed=True,
        reconcile_shed=True, optional_writes_shed=True,
        audit_detail_reduced=True,
    ),
    EmergencyLevel.FAIL_CLOSED: CapabilitySet(
        background_indexing_shed=True, qdrant_reindex_shed=True,
        reconcile_shed=True, optional_writes_shed=True,
        audit_detail_reduced=True, read_only_noncritical=True,
    ),
}

#: Capabilities that never shed — evidence and control must survive.
NEVER_SHED: tuple[str, ...] = (
    "critical_transport",
    "governance_audit",
    "health_signals",
)


@dataclass(frozen=True)
class DiskVerdict:
    level: EmergencyLevel
    free_bytes: int
    total_bytes: int
    free_fraction: float
    capabilities: CapabilitySet


def evaluate_disk(
    free_bytes: int,
    total_bytes: int,
    policy: EmergencyDiskPolicy | None = None,
) -> DiskVerdict:
    """Classify free disk into an emergency level + capability set."""
    policy = policy or EmergencyDiskPolicy()
    if total_bytes <= 0:
        # Unknown capacity cannot prove headroom — fail closed.
        return DiskVerdict(
            EmergencyLevel.FAIL_CLOSED, free_bytes, total_bytes, 0.0,
            _LEVEL_SHED[EmergencyLevel.FAIL_CLOSED],
        )
    fraction = free_bytes / total_bytes
    if fraction <= policy.fail_closed_free_fraction:
        level = EmergencyLevel.FAIL_CLOSED
    elif fraction <= policy.critical_free_fraction:
        level = EmergencyLevel.CRITICAL
    elif fraction <= policy.warning_free_fraction:
        level = EmergencyLevel.WARNING
    else:
        level = EmergencyLevel.NORMAL
    return DiskVerdict(
        level, free_bytes, total_bytes, fraction, _LEVEL_SHED[level]
    )


__all__ = [
    "CapabilitySet",
    "DiskVerdict",
    "EmergencyDiskPolicy",
    "EmergencyLevel",
    "NEVER_SHED",
    "evaluate_disk",
]
