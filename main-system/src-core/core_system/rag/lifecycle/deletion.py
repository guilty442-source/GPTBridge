"""Deletion Guarantee — tombstone first, physical removal later.

    DELETE requested
      -> PostgreSQL TOMBSTONED        (immediately invisible)
      -> query barrier                (PG blocks stale Qdrant hits)
      -> outbox delete event
      -> Qdrant delete points         (may fail — safe: barrier holds)
      -> SQLite degraded cache delete
      -> retrieval cache invalidate
      -> derived knowledge invalidate (provenance cascade)
      -> memory references evaluate
      -> verify
      -> DELETED

If Qdrant is offline the physical step retries via outbox, but the
resource is already unfindable — "deleted but still searchable" is
structurally impossible.

Derived knowledge cascades through ``provenance_edge``:

    Source A -> Summary B -> Decision Note C
    A deleted => B INVALID, C REVIEW_REQUIRED

Memory deletion is scoped — never one call wipes all tiers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class DeletionState(str, Enum):
    REQUESTED = "REQUESTED"
    TOMBSTONED = "TOMBSTONED"       # invisible; physical steps pending
    PURGING = "PURGING"
    DELETED = "DELETED"
    PURGE_FAILED = "PURGE_FAILED"   # still tombstoned — safe


class DeletionStep(str, Enum):
    PG_TOMBSTONE = "pg_tombstone"
    OUTBOX_EVENT = "outbox_event"
    QDRANT_DELETE = "qdrant_delete"
    SQLITE_CACHE_DELETE = "sqlite_cache_delete"
    RETRIEVAL_CACHE_INVALIDATE = "retrieval_cache_invalidate"
    DERIVED_INVALIDATE = "derived_invalidate"
    MEMORY_EVALUATE = "memory_evaluate"
    VERIFY = "verify"


DELETION_SEQUENCE: tuple[DeletionStep, ...] = tuple(DeletionStep)


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    resource_id: str
    state: DeletionState
    completed_steps: tuple[DeletionStep, ...] = ()
    failed_step: DeletionStep | None = None
    requested_at: float = 0.0


class DerivedRelation(str, Enum):
    INVALID = "INVALID"               # source deleted
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # source updated / upstream invalidated
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    source_resource_id: str
    derived_resource_id: str
    relation: str                     # summarizes | cites | decides | ...


def cascade_invalidation(
    deleted_or_updated: str,
    edges: list[ProvenanceEdge],
    *,
    deleted: bool = True,
) -> dict[str, DerivedRelation]:
    """BFS over provenance edges — first hop INVALID (or STALE on
    update), deeper hops REVIEW_REQUIRED."""
    result: dict[str, DerivedRelation] = {}
    first = DerivedRelation.INVALID if deleted else DerivedRelation.STALE
    frontier = {deleted_or_updated}
    depth = 0
    while frontier:
        nxt: set[str] = set()
        for e in edges:
            if e.source_resource_id in frontier and \
                    e.derived_resource_id not in result:
                result[e.derived_resource_id] = (
                    first if depth == 0 else DerivedRelation.REVIEW_REQUIRED
                )
                nxt.add(e.derived_resource_id)
        frontier = nxt
        depth += 1
    return result


class MemoryDeletionScope(str, Enum):
    SESSION = "DELETE_SESSION_MEMORY"
    EPISODIC = "DELETE_EPISODIC_MEMORY"
    LONG_TERM = "DELETE_LONG_TERM_MEMORY"
    ALL_FOR_SUBJECT = "DELETE_ALL_MEMORY_FOR_SUBJECT"


@dataclass(frozen=True, slots=True)
class MemoryDeletionRequest:
    scope: MemoryDeletionScope
    subject_id: str = ""
    session_id: str = ""
    memory_kind: str = ""             # required for tiered scopes


def validate_memory_deletion(req: MemoryDeletionRequest) -> tuple[bool, str]:
    """Scoped deletion requires the identifiers its scope implies —
    a bare delete_memory() over all tiers is rejected."""
    if req.scope in (
        MemoryDeletionScope.SESSION,
        MemoryDeletionScope.EPISODIC,
        MemoryDeletionScope.LONG_TERM,
    ) and not req.memory_kind:
        return False, "tiered-scope-requires-memory_kind"
    if req.scope is MemoryDeletionScope.SESSION and not req.session_id:
        return False, "session-scope-requires-session_id"
    if req.scope is MemoryDeletionScope.ALL_FOR_SUBJECT and not req.subject_id:
        return False, "subject-scope-requires-subject_id"
    return True, "ok"


def advance_deletion(
    record: DeletionRecord, step: DeletionStep, *, ok: bool = True
) -> DeletionRecord:
    """Advance the deletion pipeline.  Tombstone first: once
    PG_TOMBSTONE completes the resource is invisible; a later step
    failing leaves it TOMBSTONED/PURGE_FAILED, never visible again."""
    import dataclasses
    expected = DELETION_SEQUENCE[len(record.completed_steps)]
    if step is not expected:
        raise ValueError(f"step order violated: expected {expected}, got {step}")
    if not ok:
        return dataclasses.replace(
            record, state=DeletionState.PURGE_FAILED, failed_step=step,
        )
    done = record.completed_steps + (step,)
    if step is DeletionStep.PG_TOMBSTONE:
        state = DeletionState.TOMBSTONED
    elif step is DeletionStep.VERIFY:
        state = DeletionState.DELETED
    else:
        state = DeletionState.PURGING
    return dataclasses.replace(record, state=state, completed_steps=done)


def query_barrier(record: DeletionRecord | None) -> bool:
    """True => the resource must be filtered from results even if a
    stale Qdrant point still exists."""
    return record is not None and record.state in (
        DeletionState.REQUESTED,
        DeletionState.TOMBSTONED,
        DeletionState.PURGING,
        DeletionState.PURGE_FAILED,
    )


__all__ = [
    "DELETION_SEQUENCE",
    "DeletionRecord",
    "DeletionState",
    "DeletionStep",
    "DerivedRelation",
    "MemoryDeletionRequest",
    "MemoryDeletionScope",
    "ProvenanceEdge",
    "advance_deletion",
    "cascade_invalidation",
    "query_barrier",
    "validate_memory_deletion",
]
