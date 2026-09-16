"""Disaster recovery — RPO/RTO and rebuild-from-zero.

The canonical index is *rebuildable*; metadata is not:

    backup priority: PostgreSQL metadata, outbox, generation state,
                     policy, source locator registry
    Qdrant:          nice to back up, but must be fully rebuildable
                     from PG metadata + locator resolvers

If deleting all of Qdrant storage cannot be recovered by the rebuild
sequence, some canonical information secretly lives only in Qdrant —
an architecture gap, not a drill result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True, slots=True)
class RecoveryTargets:
    rpo_max_indexing_events: int = 100   # max indexing events losable
    rto_seconds: int = 300               # restore to usable


class RebuildStep(str, Enum):
    DELETE_QDRANT_STORAGE = "delete_qdrant_storage"   # drill only
    START_QDRANT = "start_qdrant"
    CREATE_GENERATION = "create_generation"
    READ_PG_METADATA = "read_pg_metadata"
    RESOLVE_SOURCES = "resolve_sources"               # locator resolvers
    RECHUNK_REEMBED = "rechunk_reembed"
    VALIDATE = "validate"
    ACTIVATE = "activate"


REBUILD_SEQUENCE: tuple[RebuildStep, ...] = (
    RebuildStep.START_QDRANT,
    RebuildStep.CREATE_GENERATION,
    RebuildStep.READ_PG_METADATA,
    RebuildStep.RESOLVE_SOURCES,
    RebuildStep.RECHUNK_REEMBED,
    RebuildStep.VALIDATE,
    RebuildStep.ACTIVATE,
)


@dataclass(frozen=True, slots=True)
class BackupPriority:
    component: str
    must_backup: bool
    rebuildable: bool
    reason: str = ""


BACKUP_PRIORITIES: tuple[BackupPriority, ...] = (
    BackupPriority("pg-metadata", True, False, "structural authority"),
    BackupPriority("outbox", True, False, "unreplayed mutations"),
    BackupPriority("generation-state", True, False, "active alias lineage"),
    BackupPriority("policy", True, False, "governance config"),
    BackupPriority("locator-registry", True, False, "source identity map"),
    BackupPriority("qdrant", False, True, "rebuildable vector projection"),
    BackupPriority("sqlite-cache", False, True, "degraded-path rebuildable"),
)


@dataclass(frozen=True, slots=True)
class RebuildReport:
    steps_completed: tuple[RebuildStep, ...]
    resources_rebuilt: int
    resources_expected: int
    complete: bool
    gap: str = ""


def evaluate_rebuild(
    steps_completed: tuple[RebuildStep, ...],
    resources_rebuilt: int,
    resources_expected: int,
) -> RebuildReport:
    """Full rebuild-from-zero must replay every step and recover
    every resource; anything less exposes hidden canonical data in
    Qdrant."""
    missing = tuple(s for s in REBUILD_SEQUENCE if s not in steps_completed)
    complete = not missing and resources_rebuilt >= resources_expected
    gap = ""
    if missing:
        gap = f"missing-steps:{','.join(s.value for s in missing)}"
    elif resources_rebuilt < resources_expected:
        gap = "unrebuildable-resources:canonical-data-in-qdrant"
    return RebuildReport(
        steps_completed=steps_completed,
        resources_rebuilt=resources_rebuilt,
        resources_expected=resources_expected,
        complete=complete,
        gap=gap,
    )


__all__ = [
    "BACKUP_PRIORITIES",
    "BackupPriority",
    "REBUILD_SEQUENCE",
    "RebuildReport",
    "RebuildStep",
    "RecoveryTargets",
    "evaluate_rebuild",
]
