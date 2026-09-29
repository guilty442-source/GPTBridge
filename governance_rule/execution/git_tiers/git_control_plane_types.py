"""Control-plane enumerations, records and proposal specs (A185 split).

Extracted from ``git_control_plane`` (source-size contract): owns every
value type the control plane shares — enums, dataclass records, tunable
constants and the reason→proposal template table.  No I/O lives here.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .governance_manifest import timing as _manifest_timing

STATE_SUBDIR = "gptbridge-control-plane"
MAIN_REF = "refs/heads/main"
ORIGIN_REMOTE = "origin"

DEFAULT_PROPOSAL_TTL_SECONDS = _manifest_timing(
    "control_plane_proposal_ttl_seconds", 300.0
)
DEFAULT_LOCK_STALL_SECONDS = _manifest_timing(
    "control_plane_lock_stall_seconds", 600.0
)
# TODO(inventory): wire to manifest timings.control_plane_* (see
# git_governance_manifest.json hardcoded_inventory).
BACKPRESSURE_QUEUE_DEPTH = 25
BUSY_ACTIVE_WORKER_FRACTION = 0.75
AUDIT_LATENCY_WARN_MS = 250.0


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class GitGlobalState(str, Enum):
    HEALTHY = "HEALTHY"
    BUSY = "BUSY"
    BACKPRESSURE = "BACKPRESSURE"
    DEGRADED = "DEGRADED"
    READ_ONLY = "READ_ONLY"
    RECOVERY = "RECOVERY"
    ERROR = "ERROR"


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARN = "WARN"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class Dimension(str, Enum):
    REPOSITORY = "repository"
    WORKTREES = "worktrees"
    WORKERS = "workers"
    WATCHERS = "watchers"
    QUEUE = "queue"
    LOCKS = "locks"
    AUDIT = "audit"
    HOOKS = "hooks"
    ORIGIN = "origin"
    RECOVERY = "recovery"
    PERFORMANCE = "performance"
    STORAGE = "storage"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ReasonCode(str, Enum):
    OK = "OK"
    MAIN_DIRTY = "MAIN_DIRTY"
    DUPLICATE_WATCHER = "DUPLICATE_WATCHER"
    STALE_WATCHER = "STALE_WATCHER"
    WATCHER_MISSING = "WATCHER_MISSING"
    STALE_LOCK = "STALE_LOCK"
    LOCK_STALL = "LOCK_STALL_WARNING"
    AUDIT_CHAIN_INVALID = "AUDIT_CHAIN_INVALID"
    AUDIT_LATENCY_HIGH = "AUDIT_LATENCY_HIGH"
    ORIGIN_DIVERGED = "ORIGIN_DIVERGED"
    HOOK_HASH_MISMATCH = "HOOK_HASH_MISMATCH"
    OBJECT_CORRUPTION = "OBJECT_CORRUPTION"
    QUEUE_STARVATION = "QUEUE_STARVATION"
    QUEUE_DEPTH_HIGH = "QUEUE_DEPTH_HIGH"
    WORKER_POOL_FULL = "WORKER_POOL_FULL"
    WORKER_DIRTY = "WORKER_DIRTY"
    WORKER_DIRTY_LONG = "WORKER_DIRTY_LONG"
    DISK_PRESSURE = "DISK_PRESSURE"
    BACKUP_STALE = "BACKUP_STALE"
    SYNC_STALE = "SYNC_STALE"
    RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
    STATE_UNKNOWN = "STATE_UNKNOWN"


class EventType(str, Enum):
    WORKTREE_DIRTY = "WORKTREE_DIRTY"
    WORKTREE_CLEAN = "WORKTREE_CLEAN"
    SELF_COMMIT_COMPLETED = "SELF_COMMIT_COMPLETED"
    WORKER_ALLOCATED = "WORKER_ALLOCATED"
    WORKER_RETIRED = "WORKER_RETIRED"
    WATCHER_STARTED = "WATCHER_STARTED"
    WATCHER_FAILED = "WATCHER_FAILED"
    QUEUE_ENQUEUED = "QUEUE_ENQUEUED"
    MERGE_STARTED = "MERGE_STARTED"
    MERGE_COMPLETED = "MERGE_COMPLETED"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    AUDIT_FAILED = "AUDIT_FAILED"
    SYNC_COMPLETED = "SYNC_COMPLETED"
    HOOK_INVALID = "HOOK_INVALID"
    RECOVERY_POINT_CREATED = "RECOVERY_POINT_CREATED"
    BACKPRESSURE_ENTERED = "BACKPRESSURE_ENTERED"
    STATE_CHANGED = "STATE_CHANGED"
    HEALTH_RECOVERED = "HEALTH_RECOVERED"
    LOCK_STALL_WARNING = "LOCK_STALL_WARNING"
    PROPOSAL_CREATED = "PROPOSAL_CREATED"


class StateKind(str, Enum):
    FAST = "FAST"          # cache/events/registry — display only
    VERIFIED = "VERIFIED"  # fresh Git/locks/hooks/audit reads — action grade


# Event types that must reach the audit ledger (§185).
_AUDIT_WORTHY_EVENTS = frozenset({
    EventType.MERGE_COMPLETED, EventType.MERGE_CONFLICT,
    EventType.AUDIT_FAILED, EventType.HOOK_INVALID,
    EventType.RECOVERY_POINT_CREATED, EventType.STATE_CHANGED,
    EventType.HEALTH_RECOVERED,
})

_TIER3_MARKERS = (
    "push --force", "push -f", "push +", "reset --hard", "branch -d",
    "branch -D", "tag -d", "commit --amend", "reflog expire",
    "gc --prune=now", "prune-now", "push --delete", "replace",
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class GitEvent:
    event_type: EventType
    severity: Severity = Severity.INFO
    detail: str = ""
    worker_id: str = ""
    task_id: str = ""
    worktree: str = ""
    branch: str = ""
    revision: str = ""
    transaction_id: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "worktree": self.worktree,
            "branch": self.branch,
            "revision": self.revision,
            "transaction_id": self.transaction_id,
            "severity": self.severity.value,
            "detail": self.detail,
        }


@dataclass
class DimensionHealth:
    dimension: Dimension
    status: HealthStatus
    reason_codes: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "detail": self.detail,
        }


@dataclass
class GitSnapshot:
    """§164 unified snapshot — one control-plane read model for all UIs."""
    generation: int
    timestamp: float
    state_kind: str
    global_state: str
    repository_mode: str = "worktree"
    main_revision: str = ""
    origin_revision: str = ""
    main_dirty: bool = False
    worktree_count: int = 0
    worker_count: int = 0
    active_workers: int = 0
    dirty_workers: int = 0
    quarantined_workers: int = 0
    watcher_count: int = 0
    duplicate_watchers: int = 0
    stale_watchers: int = 0
    queue_depth: int = 0
    running_merge: str = ""
    oldest_queue_age: float = 0.0
    active_locks: int = 0
    stale_locks: int = 0
    audit_sequence: int = 0
    audit_chain_valid: bool = True
    audit_latency_ms: float = 0.0
    hook_health: str = HealthStatus.HEALTHY.value
    last_successful_sync: float = 0.0
    last_successful_merge: float = 0.0
    last_successful_bundle: float = 0.0
    repository_object_health: str = HealthStatus.HEALTHY.value
    disk_pressure: bool = False
    remote_relations: dict[str, str] = field(default_factory=dict)
    performance_summary: dict[str, Any] = field(default_factory=dict)
    health: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items()}


@dataclass
class ActionProposal:
    """§173 — a proposal, never an execution."""
    proposal_id: str
    created_at: float
    based_on_generation: int
    reason_code: str
    description: str
    command_plan: list[str]
    risk_tier: int                       # 1 | 2 | 3
    required_approval: str               # "none" | "confirmation" | "authority"
    affected_worktrees: list[str] = field(default_factory=list)
    affected_refs: list[str] = field(default_factory=list)
    recovery_anchor: str = ""
    expires_at: float = 0.0
    tier_policy_version: str = ""
    tier_policy_digest: str = ""
    hook_digest_set: str = ""

    def is_tier3(self) -> bool:
        return self.risk_tier >= 3

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items()}


@dataclass
class CapabilityToken:
    """§199-200 — proposal-scoped, single-use authority."""
    token_id: str
    actor: str
    operation: str
    repository: str
    worktree: str = ""
    ref: str = ""
    proposal_id: str = ""
    generation: int = 0
    expires_at: float = 0.0
    nonce: str = ""
    used_at: float = 0.0
    result: str = ""
    command_id: str = ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _severity_for_status(status: str) -> Severity:
    return {
        HealthStatus.WARN.value: Severity.WARN,
        HealthStatus.DEGRADED.value: Severity.DEGRADED,
        HealthStatus.ERROR.value: Severity.ERROR,
        HealthStatus.UNKNOWN.value: Severity.WARN,
    }.get(status, Severity.INFO)


# Reason -> proposal template.  Tier 3 proposals exist only as
# PROPOSAL_PENDING_AUTHORITY — the control plane can never run them.
_PROPOSAL_SPECS: dict[str, dict[str, Any]] = {
    ReasonCode.MAIN_DIRTY.value: {
        "description": "main worktree is dirty; surface for operator",
        "command_plan": [], "risk_tier": 2,
        "affected_refs": [MAIN_REF],
    },
    ReasonCode.STALE_LOCK.value: {
        "description": "stale lock detected; propose governed lock cleanup",
        "command_plan": ["inspect lock owner", "verify stale", "clear lock"],
        "risk_tier": 2,
    },
    ReasonCode.DUPLICATE_WATCHER.value: {
        "description": "duplicate watcher; propose retiring one watcher",
        "command_plan": ["identify duplicate watcher", "retire duplicate"],
        "risk_tier": 2,
    },
    ReasonCode.QUEUE_DEPTH_HIGH.value: {
        "description": "merge queue backpressure; pause new allocations",
        "command_plan": ["pause worker allocation"],
        "risk_tier": 1,
    },
    ReasonCode.ORIGIN_DIVERGED.value: {
        "description": "origin diverged from local main",
        "command_plan": ["inspect divergence", "propose reconcile plan"],
        "risk_tier": 3, "affected_refs": [MAIN_REF],
    },
    ReasonCode.OBJECT_CORRUPTION.value: {
        "description": "repository object corruption; propose recovery",
        "command_plan": ["fsck detail", "propose restore-from-bundle"],
        "risk_tier": 3, "affected_refs": [MAIN_REF],
    },
    ReasonCode.AUDIT_CHAIN_INVALID.value: {
        "description": "audit chain invalid; enter READ_ONLY and escalate",
        "command_plan": ["freeze governed writes", "operator audit repair"],
        "risk_tier": 3,
    },
    ReasonCode.BACKUP_STALE.value: {
        "description": "recovery artifacts stale; propose new bundle",
        "command_plan": ["create bundle"], "risk_tier": 2,
    },
}


__all__ = [
    "ActionProposal", "CapabilityToken", "Dimension", "DimensionHealth",
    "EventType", "GitEvent", "GitGlobalState", "GitSnapshot", "HealthStatus",
    "ReasonCode", "Severity", "StateKind",
    "AUDIT_LATENCY_WARN_MS", "BACKPRESSURE_QUEUE_DEPTH",
    "BUSY_ACTIVE_WORKER_FRACTION", "DEFAULT_LOCK_STALL_SECONDS",
    "DEFAULT_PROPOSAL_TTL_SECONDS", "MAIN_REF", "ORIGIN_REMOTE",
    "STATE_SUBDIR",
]
