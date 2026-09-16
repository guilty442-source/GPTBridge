"""SQLite fallback saga and conflict resolution.

While PostgreSQL is offline the module records the operation locally
(``local_outbox`` semantics) with its idempotency key and pending sync
marker.  SQLite may never declare a central operation complete: when PG
returns, local operations are reconciled and the central authority decides
complete / conflict.

Conflicts are never resolved by timestamp (no last-write-wins); authority,
generation, revision and operation lineage decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class LocalOperationStatus(Enum):
    LOCAL_PENDING = "LOCAL_PENDING"
    PENDING_SYNC = "PENDING_SYNC"
    RECONCILING = "RECONCILING"
    SYNCED = "SYNCED"
    CONFLICT = "CONFLICT"


class FallbackSagaError(RuntimeError):
    """Raised when the local fallback tries to exceed its authority."""


@dataclass
class LocalOperation:
    operation_id: str
    idempotency_key: str
    resource_id: str
    local_revision: str
    operation_type: str = ""
    status: LocalOperationStatus = LocalOperationStatus.LOCAL_PENDING
    pending_sync: bool = True
    result: dict[str, object] = field(default_factory=dict)

    def mark_synced(self) -> None:
        raise FallbackSagaError("SQLITE_CANNOT_DECLARE_CENTRAL_COMPLETION")

    def mark_pending_sync(self, via: str) -> None:
        if not via.startswith("reconcile:"):
            raise FallbackSagaError("SQLITE_SYNC_REQUIRES_RECONCILE_PATH")
        self.status = LocalOperationStatus.PENDING_SYNC
        self.pending_sync = True


class ConflictResolution(Enum):
    LOCAL_WINS = "local-wins"
    CENTRAL_WINS = "central-wins"
    SUPERSEDE = "supersede"
    REQUIRE_HUMAN = "require-human"


AUTHORITY_ORDER: Final[tuple[str, ...]] = (
    "canonical",  # PostgreSQL central authority
    "module-private",
    "degraded-copy",
)


@dataclass(frozen=True)
class ConflictFacts:
    local_revision: str
    central_revision: str
    local_generation: int
    central_generation: int
    local_authority: str
    central_authority: str
    lineage_known: bool = True


def resolve_conflict(facts: ConflictFacts) -> ConflictResolution:
    """Deterministic conflict policy — never timestamp-based."""
    if not facts.lineage_known:
        return ConflictResolution.REQUIRE_HUMAN
    if facts.local_generation != facts.central_generation:
        newer = (
            ConflictResolution.LOCAL_WINS
            if facts.local_generation > facts.central_generation
            else ConflictResolution.CENTRAL_WINS
        )
        return newer
    if facts.local_revision == facts.central_revision:
        return ConflictResolution.CENTRAL_WINS
    local_rank = AUTHORITY_ORDER.index(facts.local_authority) if facts.local_authority in AUTHORITY_ORDER else 99
    central_rank = AUTHORITY_ORDER.index(facts.central_authority) if facts.central_authority in AUTHORITY_ORDER else 99
    if local_rank < central_rank:
        return ConflictResolution.LOCAL_WINS
    if central_rank < local_rank:
        return ConflictResolution.CENTRAL_WINS
    return ConflictResolution.SUPERSEDE


__all__ = [
    "AUTHORITY_ORDER",
    "ConflictFacts",
    "ConflictResolution",
    "FallbackSagaError",
    "LocalOperation",
    "LocalOperationStatus",
    "resolve_conflict",
]
