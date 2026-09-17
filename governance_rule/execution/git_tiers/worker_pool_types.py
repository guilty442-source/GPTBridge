"""Worker Pool type model (task §38/§39/§70, A375).

Every worker slot is a typed record — worker state is a closed enum,
never free text.  Capacity is declared in ``PoolConfig`` which is
loaded from the governed pool config file (deployment binding, A386)
rather than scattered through code.

Pool split:
    persistent — git / local-model / rag / ui; never auto-retired
    ephemeral  — dynamic AI task workers w001..wNN

``main`` is never a worker: it is the integration target only.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .branch_policy import MAIN_BRANCH, PROTECTED_BRANCHES
from .governance_manifest import timing as _manifest_timing

# Governed pool defaults (manifest version source; A318-A320).
_WORKER_LEASE_SECONDS: float = _manifest_timing(
    "worker_pool_lease_seconds", 1800.0
)
_WORKER_LEASE_RENEW_SECONDS: float = _manifest_timing(
    "worker_pool_lease_renew_seconds", 300.0
)
_MERGED_BRANCH_RETENTION_SECONDS: float = _manifest_timing(
    "worker_pool_merged_branch_retention_seconds", 72 * 3600.0
)
_BACKPRESSURE_QUEUE_DEPTH: int = int(
    _manifest_timing("worker_pool_backpressure_queue_depth", 50)
)


class PoolType(Enum):
    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"


class WorkerState(Enum):
    FREE = "FREE"
    ALLOCATING = "ALLOCATING"
    READY = "READY"
    WORKING = "WORKING"
    DIRTY = "DIRTY"
    COMMITTED = "COMMITTED"
    QUEUED = "QUEUED"
    MERGING = "MERGING"
    MERGED = "MERGED"
    SYNCING = "SYNCING"
    RETIRING = "RETIRING"
    RECYCLING = "RECYCLING"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


#: States a persistent worker may occupy — RETIRING/RECYCLING are
#: forbidden for persistent members (§53) unless a human governor
#: performs the operation outside this machine.
PERSISTENT_STATES: frozenset[WorkerState] = frozenset(
    {
        WorkerState.READY,
        WorkerState.WORKING,
        WorkerState.DIRTY,
        WorkerState.COMMITTED,
        WorkerState.SYNCING,
        WorkerState.FAILED,
        WorkerState.QUEUED,
        WorkerState.MERGING,
        WorkerState.MERGED,
        WorkerState.FREE,
        WorkerState.ALLOCATING,
    }
)

#: Legal state transitions.  Anything not listed is rejected.
WORKER_TRANSITIONS: dict[WorkerState, frozenset[WorkerState]] = {
    WorkerState.FREE: frozenset({WorkerState.ALLOCATING, WorkerState.RETIRING}),
    WorkerState.ALLOCATING: frozenset(
        {WorkerState.READY, WorkerState.FAILED, WorkerState.FREE}
    ),
    WorkerState.READY: frozenset(
        {WorkerState.WORKING, WorkerState.RECYCLING, WorkerState.RETIRING,
         WorkerState.QUARANTINED}
    ),
    WorkerState.WORKING: frozenset(
        {WorkerState.DIRTY, WorkerState.COMMITTED, WorkerState.FAILED,
         WorkerState.QUARANTINED}
    ),
    WorkerState.DIRTY: frozenset(
        {WorkerState.COMMITTED, WorkerState.WORKING, WorkerState.QUARANTINED}
    ),
    WorkerState.COMMITTED: frozenset(
        {WorkerState.QUEUED, WorkerState.WORKING, WorkerState.DIRTY,
         WorkerState.QUARANTINED}
    ),
    WorkerState.QUEUED: frozenset(
        {WorkerState.MERGING, WorkerState.WORKING, WorkerState.COMMITTED,
         WorkerState.QUARANTINED}
    ),
    WorkerState.MERGING: frozenset(
        {WorkerState.MERGED, WorkerState.QUEUED, WorkerState.FAILED,
         WorkerState.QUARANTINED}
    ),
    WorkerState.MERGED: frozenset(
        {WorkerState.SYNCING, WorkerState.RETIRING}
    ),
    WorkerState.SYNCING: frozenset(
        {WorkerState.RETIRING, WorkerState.READY, WorkerState.QUARANTINED}
    ),
    WorkerState.RETIRING: frozenset(
        {WorkerState.FREE, WorkerState.QUARANTINED}
    ),
    WorkerState.RECYCLING: frozenset(
        {WorkerState.READY, WorkerState.QUARANTINED, WorkerState.FREE}
    ),
    WorkerState.FAILED: frozenset(
        {WorkerState.QUARANTINED, WorkerState.RECYCLING, WorkerState.RETIRING}
    ),
    WorkerState.QUARANTINED: frozenset(
        {WorkerState.RECYCLING, WorkerState.RETIRING}
    ),
}


class AllocationResult(Enum):
    ALLOCATED = "ALLOCATED"
    WAITING_FOR_WORKER = "WAITING_FOR_WORKER"
    BACKPRESSURE = "BACKPRESSURE"
    POOL_CAPACITY_STORAGE_LIMIT = "POOL_CAPACITY_STORAGE_LIMIT"
    POOL_DRAINING = "POOL_DRAINING"
    DENIED = "DENIED"


class PoolHealth(Enum):
    HEALTHY = "HEALTHY"
    BUSY = "BUSY"
    BACKPRESSURE = "BACKPRESSURE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    DRAINING = "POOL_DRAINING"
    STOPPED = "STOPPED"


class ReconcileClass(Enum):
    VALID = "VALID"
    STALE = "STALE"
    ORPHAN = "ORPHAN"
    QUARANTINED = "QUARANTINED"


@dataclass
class PoolConfig:
    """Governed pool capacity (§39).  Loaded from the pool config
    file — these defaults exist so a fresh repository can boot."""

    schema_version: int = 1
    # TODO(inventory): capacity/parallelism limits stay pool policy values
    # (see git_governance_manifest.json hardcoded_inventory).
    max_ephemeral_workers: int = 24
    max_total_workers: int = 32
    max_parallel_self_commit: int = 4
    max_parallel_git_reads: int = 16
    max_parallel_merge: int = 1
    warm_workers: int = 4
    workers_root: str = ""          # resolved per deployment
    persistent_workers: tuple[str, ...] = tuple(
        sorted(PROTECTED_BRANCHES - {MAIN_BRANCH})
    )
    worker_lease_seconds: float = _WORKER_LEASE_SECONDS
    worker_lease_renew_seconds: float = _WORKER_LEASE_RENEW_SECONDS
    merged_branch_retention_seconds: float = _MERGED_BRANCH_RETENTION_SECONDS
    worktree_storage_limit_bytes: int = 64 * 1024 ** 3
    backpressure_queue_depth: int = _BACKPRESSURE_QUEUE_DEPTH
    backpressure_free_fraction: float = 0.10


@dataclass
class WorkerSlot:
    """One pool slot (§38).  ``state`` is always ``WorkerState``."""

    worker_id: str
    instance_id: str                          # §68: w007:<uuid>
    pool_type: PoolType
    state: WorkerState
    task_id: str = ""
    attempt_id: str = ""
    display_name: str = ""
    worktree_path: str = ""
    branch: str = ""
    base_branch: str = "main"
    base_revision: str = ""
    head_revision: str = ""
    pid: int = 0
    watcher_pid: int = 0
    created_at: float = 0.0
    allocated_at: float = 0.0
    last_activity: float = 0.0
    lease_expires_at: float = 0.0
    priority: int = 0
    claim_count: int = 0
    queue_id: str = ""
    domain: str = ""
    parent_task_id: str = ""                  # §47 cross-domain
    retired_at: float = 0.0
    merged_retained_until: float = 0.0

    @property
    def ownership_key(self) -> str:
        """True ownership identity: ``w007:<instance-id>`` (§68)."""
        return f"{self.worker_id}:{self.instance_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pool_type"] = self.pool_type.value
        data["state"] = self.state.value
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "WorkerSlot":
        payload = dict(data)
        payload["pool_type"] = PoolType(payload["pool_type"])
        payload["state"] = WorkerState(payload["state"])
        known = {f for f in WorkerSlot.__dataclass_fields__}
        return WorkerSlot(**{k: v for k, v in payload.items() if k in known})


def new_instance_id() -> str:
    return uuid.uuid4().hex


def can_transition(slot: WorkerSlot, target: WorkerState) -> bool:
    """Enum-guarded transition check (§38: no free-text states)."""
    if target not in WORKER_TRANSITIONS.get(slot.state, frozenset()):
        return False
    if slot.pool_type is PoolType.PERSISTENT:
        return target in PERSISTENT_STATES
    return True


__all__ = [
    "AllocationResult",
    "PERSISTENT_STATES",
    "PoolConfig",
    "PoolHealth",
    "PoolType",
    "ReconcileClass",
    "WORKER_TRANSITIONS",
    "WorkerSlot",
    "WorkerState",
    "can_transition",
    "new_instance_id",
]
