"""Capacity Certification (release gate, A369).

Before a Database Release activates, capacity must be proven — not
just "disk has free space" but the five transient peaks a release
actually creates:

    peak_storage          — projected data at the release's peak
    migration_temp_space  — copy/rewrite headroom migrations need
    backup_temp_space     — pre-release backup must fit
    qdrant_rebuild_space  — dual-collection coexistence if the
                            release rebuilds vectors
    wal_headroom          — WAL burst during the migration window

All five must fit within measured free space with a safety margin.
A miss fails the gate — the release cannot activate on hope.

Usage:
    from shared_layer.database.capacity_certification import (
        CapacityRequirement, certify_release_capacity,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CertStatus(Enum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class CapacityRequirement:
    """The five transient peaks a release must survive."""

    peak_storage_bytes: int
    migration_temp_bytes: int
    backup_temp_bytes: int
    qdrant_rebuild_bytes: int
    wal_headroom_bytes: int
    safety_margin: float = 0.10  # extra fraction required on top

    def __post_init__(self) -> None:
        if not 0.0 <= self.safety_margin < 1.0:
            raise ValueError("safety_margin must be in [0, 1)")


@dataclass(frozen=True)
class RequirementVerdict:
    name: str
    required_bytes: int
    available_bytes: int
    passed: bool


@dataclass(frozen=True)
class CapacityCertification:
    status: CertStatus
    verdicts: tuple[RequirementVerdict, ...]
    free_bytes: int
    margin: float

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.verdicts if not v.passed)


def certify_release_capacity(
    requirement: CapacityRequirement,
    *,
    free_bytes: int,
    dedicated_migration_bytes: int = 0,
    dedicated_backup_bytes: int = 0,
) -> CapacityCertification:
    """Verify each requirement against available space.

    ``dedicated_*`` bytes are reservations outside ``free_bytes``
    (e.g. a separate backup volume); they relax the corresponding
    requirement against the main volume only.
    """
    margin = requirement.safety_margin
    checks = (
        ("peak_storage", requirement.peak_storage_bytes, free_bytes),
        ("migration_temp_space", requirement.migration_temp_bytes,
         free_bytes + dedicated_migration_bytes),
        ("backup_temp_space", requirement.backup_temp_bytes,
         free_bytes + dedicated_backup_bytes),
        ("qdrant_rebuild_space", requirement.qdrant_rebuild_bytes,
         free_bytes),
        ("wal_headroom", requirement.wal_headroom_bytes, free_bytes),
    )
    verdicts = tuple(
        RequirementVerdict(
            name, required, available,
            required * (1.0 + margin) <= available,
        )
        for name, required, available in checks
    )
    status = (
        CertStatus.PASS
        if all(v.passed for v in verdicts)
        else CertStatus.FAIL
    )
    return CapacityCertification(status, verdicts, free_bytes, margin)


__all__ = [
    "CapacityCertification",
    "CapacityRequirement",
    "CertStatus",
    "RequirementVerdict",
    "certify_release_capacity",
]
