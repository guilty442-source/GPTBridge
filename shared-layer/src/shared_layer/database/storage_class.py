"""Storage Class Registry (A369 DATA-TIERS).

Every data domain carries a storage class:

    hot         — recent / high-frequency (PostgreSQL central index)
    warm        — normal history (SQLite checkpoints, recent audit)
    archive     — long retention, immutable (audit archive, backups)
    rebuildable — derivable projection (Qdrant vectors, caches)

Codex lifecycle tiers Hot/Warm/Archive/Tombstone/Purged map onto the
retention state machine (lifecycle_manager); this registry describes
*where bytes live and how they may be treated*, not lifecycle state.

Transition guard: a class change must preserve lifecycle, revision,
locator, ACL, retention and audit binding — and can never override a
retention hold or authority (A369).

Usage:
    from shared_layer.database.storage_class import (
        StorageClass, classify, can_transition,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StorageClass(Enum):
    HOT = "hot"
    WARM = "warm"
    ARCHIVE = "archive"
    REBUILDABLE = "rebuildable"


#: Canonical classification of the platform's data domains.
#: Domain keys are semantic codes; physical resolution is A386-bound.
DOMAIN_STORAGE_CLASS: dict[str, StorageClass] = {
    "pg_central_index": StorageClass.HOT,
    "pg_transport_hot": StorageClass.HOT,
    "pg_audit_hot": StorageClass.HOT,
    "sqlite_operational": StorageClass.WARM,
    "sqlite_checkpoint": StorageClass.WARM,
    "audit_archive": StorageClass.ARCHIVE,
    "transport_archive": StorageClass.ARCHIVE,
    "backup": StorageClass.ARCHIVE,
    "qdrant_vector": StorageClass.REBUILDABLE,
    "derived_cache": StorageClass.REBUILDABLE,
}

#: Allowed class transitions.  Hot data cools downward; archive is a
#: terminal class (content may be read but never reclassified hot);
#: rebuildable data may re-enter hot only through a verified rebuild.
_ALLOWED: dict[StorageClass, frozenset[StorageClass]] = {
    StorageClass.HOT: frozenset(
        {StorageClass.WARM, StorageClass.ARCHIVE}
    ),
    StorageClass.WARM: frozenset(
        {StorageClass.HOT, StorageClass.ARCHIVE}
    ),
    StorageClass.ARCHIVE: frozenset(),
    StorageClass.REBUILDABLE: frozenset(
        {StorageClass.HOT, StorageClass.WARM, StorageClass.ARCHIVE}
    ),
}


@dataclass(frozen=True)
class TransitionVerdict:
    domain: str
    source: StorageClass
    target: StorageClass
    allowed: bool
    reason: str


def classify(domain: str) -> StorageClass:
    """Return the declared storage class for a domain."""
    try:
        return DOMAIN_STORAGE_CLASS[domain]
    except KeyError:
        raise KeyError(f"unclassified storage domain: {domain}")


def can_transition(
    domain: str,
    target: StorageClass,
    *,
    retention_hold: bool = False,
) -> TransitionVerdict:
    """Decide whether a domain may move to ``target`` class.

    A retention hold freezes classification — data under hold must not
    cool into archive/rebuildable treatment regardless of access
    frequency (A369).
    """
    source = classify(domain)
    if retention_hold:
        return TransitionVerdict(
            domain, source, target, False, "retention-hold-active"
        )
    if target not in _ALLOWED[source]:
        return TransitionVerdict(
            domain, source, target, False,
            f"transition-denied:{source.value}->{target.value}",
        )
    return TransitionVerdict(domain, source, target, True, "allowed")


__all__ = [
    "DOMAIN_STORAGE_CLASS",
    "StorageClass",
    "TransitionVerdict",
    "can_transition",
    "classify",
]
