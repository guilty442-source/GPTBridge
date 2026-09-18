"""Git Control Plane — unified read-only aggregation of Git runtime state.

§162-217.  The control plane observes, judges and proposes — it never
executes Git writes itself:

    collect_*()          -> dimensional facts (per source of truth)
    evaluate_global_state() -> deterministic HEALTHY..ERROR
    snapshot()           -> Unified Git Snapshot with a generation counter
    build_action_proposals() -> ActionProposal (expires, generation-bound)
    request_action()     -> validate + record decision; NEVER executes.
                            Tier 3 always returns PROPOSAL_PENDING_AUTHORITY.

Sources of truth are strictly separated (§166): Git refs/HEAD for
revisions, the pool registry for workers, the merge queue for
integration state, the audit chain for governed evidence, the recovery
store for recovery evidence.  An event bus (§167-168) invalidates the
fast-state cache but never replaces verified Git state (§169).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from . import audit_chain
from . import automation_supervisor_state
from . import git_perf
from .audit_chain import chain_health, verify_tail
from .git_repository import GitRepository
from .repo_sync import sync_state
from .governance_manifest import timing as _manifest_timing
from .merge_queue import MergeQueue
from .worker_registry import PoolRegistry

_logger_name = "gptbridge.git.control-plane"

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
# Control Plane
# ---------------------------------------------------------------------------


class GitControlPlane:
    """Aggregates Git runtime state; proposes — never executes — actions.

    All collectors are dependency-injectable: pass ``sources`` mapping
    name -> callable to override ``collect_<name>`` (tests inject fakes;
    production uses the real Git / registry / queue / audit surfaces).
    """

    _COLLECTORS = (
        "state", "health", "workers", "queue", "locks",
        "audit", "remotes", "recovery", "performance",
    )

    def __init__(
        self,
        root: str | Path,
        *,
        sources: Optional[dict[str, Callable[[], dict[str, Any]]]] = None,
        state_dir: Optional[Path] = None,
        proposal_ttl: float = DEFAULT_PROPOSAL_TTL_SECONDS,
        lock_stall_seconds: float = DEFAULT_LOCK_STALL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).resolve()
        self._now = now
        self._proposal_ttl = proposal_ttl
        self._lock_stall_seconds = lock_stall_seconds
        self._lock = threading.Lock()
        self._generation = 0
        self._fast_snapshot: Optional[GitSnapshot] = None
        self._events: list[GitEvent] = []
        self._event_sink: Optional[Callable[[GitEvent], None]] = None
        self._alerts: dict[str, dict[str, Any]] = {}
        self._decisions: list[dict[str, Any]] = []
        self._proposals: dict[str, ActionProposal] = {}
        self._capabilities: dict[str, CapabilityToken] = {}
        self._commands: list[dict[str, Any]] = []
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._prev_dimension_status: dict[str, str] = {}
        self._state_dir = state_dir or (
            automation_supervisor_state._state_dir(self.root).parent
            / STATE_SUBDIR
        )
        self._sources: dict[str, Callable[[], dict[str, Any]]] = {}
        for name in self._COLLECTORS:
            override = (sources or {}).get(name)
            self._sources[name] = override or getattr(self, f"_collect_{name}")

    # -- event bus ----------------------------------------------------------

    def emit(self, event: GitEvent) -> GitEvent:
        """Append an event.  Events invalidate fast state; they never
        replace verified Git state (§168)."""
        self._events.append(event)
        self._fast_snapshot = None  # event -> cache invalidation
        if self._event_sink is not None:
            self._event_sink(event)
        if event.event_type in _AUDIT_WORTHY_EVENTS:
            try:
                audit_chain.append_audit({
                    "operation": "control-plane-event",
                    "event": event.to_dict(),
                })
            except Exception:
                pass
        return event

    def set_event_sink(self, sink: Callable[[GitEvent], None]) -> None:
        self._event_sink = sink

    def events(
        self, *, event_type: Optional[EventType] = None,
        transaction_id: str = "", limit: int = 200,
    ) -> list[GitEvent]:
        out = self._events
        if event_type is not None:
            out = [e for e in out if e.event_type is event_type]
        if transaction_id:
            out = [e for e in out if e.transaction_id == transaction_id]
        return out[-limit:]

    # -- collectors (sources of truth, §166) ---------------------------------

    def _collect_state(self) -> dict[str, Any]:
        """Git truth: HEAD / refs / index / worktrees."""
        repo = GitRepository(self.root)
        head = repo.run(["rev-parse", "--verify", MAIN_REF])
        main_sha = head.stdout.strip() if head.returncode == 0 else ""
        porcelain = repo.run(["status", "--porcelain"])
        dirty = bool((porcelain.stdout or "").strip()) and porcelain.returncode == 0
        wt = repo.run(["worktree", "list", "--porcelain"])
        worktrees = [
            line.split(" ", 1)[1]
            for line in (wt.stdout or "").splitlines()
            if line.startswith("worktree ")
        ]
        fsck = repo.run(["fsck", "--no-dangling"], timeout=60)
        object_health = (
            HealthStatus.HEALTHY.value if fsck.returncode == 0
            else HealthStatus.ERROR.value
        )
        return {
            "main_revision": main_sha,
            "main_dirty": dirty,
            "worktrees": worktrees,
            "worktree_count": len(worktrees),
            "repository_mode": "worktree",
            "repository_object_health": object_health,
            "unknown_main_ref": not main_sha,
        }

    def _collect_workers(self) -> dict[str, Any]:
        """Supervisor registry truth: workers / watchers / leases."""
        directory = (
            automation_supervisor_state._state_dir(self.root) / "worker-pool"
        )
        registry = PoolRegistry(directory)
        try:
            slots = registry.load_slots()
        except Exception:
            slots = {}
        workers = list(slots.values())
        active = sum(1 for w in workers if str(w.state).endswith("ACTIVE")
                     or getattr(w.state, "value", "") == "ACTIVE")
        quarantined = sum(
            1 for w in workers
            if getattr(w.state, "value", str(w.state)) in ("QUARANTINED",)
        )
        watcher_pids = [w.watcher_pid for w in workers if w.watcher_pid]
        seen: set[int] = set()
        dup = sum(1 for p in watcher_pids if p in seen or seen.add(p))
        dirty_workers = [
            w for w in workers
            if getattr(w.state, "value", str(w.state)) == "DIRTY"
        ]
        return {
            "worker_count": len(workers),
            "active_workers": active,
            "dirty_workers": len(dirty_workers),
            "quarantined_workers": quarantined,
            "watcher_count": len(watcher_pids),
            "duplicate_watchers": dup,
            "stale_watchers": 0,
            "workers": [vars(w) for w in workers],
        }

    def _collect_queue(self) -> dict[str, Any]:
        """Merge queue truth: integration state."""
        queue = MergeQueue(self.root)
        stats = queue.stats()
        pending = queue.entries(statuses=("pending", "queued"))
        running = queue.entries(statuses=("running",))
        oldest = 0.0
        if pending:
            oldest = self._now() - min(
                float(e.get("enqueued_at", self._now())) for e in pending
            )
        return {
            "queue_depth": int(stats.get("pending", 0) or stats.get("queued", 0)
                               or len(pending)),
            "running_merge": (running[0].get("queue_id", "")
                              if running else ""),
            "oldest_queue_age": oldest,
            "stats": stats,
        }

    def _collect_locks(self) -> dict[str, Any]:
        """Lock truth: supervisor + pool lock files."""
        state_dir = automation_supervisor_state._state_dir(self.root)
        locks: list[dict[str, Any]] = []
        now = self._now()
        for path in state_dir.glob("**/*.lock"):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            locks.append({
                "name": path.name,
                "path": str(path),
                "age_seconds": age,
                "stale": age > self._lock_stall_seconds,
            })
        return {
            "active_locks": len(locks),
            "stale_locks": sum(1 for l in locks if l["stale"]),
            "locks": locks,
        }

    def _collect_audit(self) -> dict[str, Any]:
        """Audit truth: governed operation evidence."""
        started = time.perf_counter()
        health = chain_health()
        latency_ms = (time.perf_counter() - started) * 1000.0
        return {
            "audit_sequence": int(health.get("audit_record_count", 0) or 0),
            "audit_chain_valid": bool(health.get("chain_valid", False)),
            "audit_latency_ms": latency_ms,
            "audit_size": health.get("audit_size", 0),
            "last_rotation": health.get("last_rotation", ""),
        }

    def _collect_remotes(self) -> dict[str, Any]:
        """Origin truth via ancestor checks (§181)."""
        state = sync_state(self.root, live_remote=False)
        return {
            "origin_revision": state.get("origin_main_sha") or "",
            "remote_relations": state.get("relations", {}),
        }

    def _collect_recovery(self) -> dict[str, Any]:
        """Recovery store truth."""
        try:
            from . import recovery as recovery_mod
            refs = recovery_mod.list_recovery_refs(GitRepository(self.root))
        except Exception:
            refs = []
        return {
            "recovery_refs": len(refs),
            "recovery_in_progress": False,
        }

    def _collect_performance(self) -> dict[str, Any]:
        """Performance truth: git command timings."""
        return {"performance_summary": git_perf.snapshot(self.root)}

    def _collect_health(self) -> dict[str, Any]:
        """Hook + disk health (folded into dimensional evaluation)."""
        return {
            "hook_digest_set": self.hook_digest(),
            "disk_pressure": self._disk_pressure(),
        }

    # -- hook + policy binding ----------------------------------------------

    def hook_digest(self) -> str:
        """Digest over the live hook set (§204)."""
        git_dir = automation_supervisor_state._state_dir(self.root).parent
        hooks_dir = git_dir / "hooks"
        if not hooks_dir.is_dir():
            return ""
        digest = hashlib.sha256()
        for path in sorted(hooks_dir.iterdir()):
            if path.is_file() and not path.name.endswith(".sample"):
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def policy_digest(self) -> str:
        """Digest of the tier classification policy (§203)."""
        from . import TIER1_OPS, TIER2_OPS, TIER3_OPS  # noqa
        payload = json.dumps(
            [sorted(TIER1_OPS), sorted(TIER2_OPS), sorted(TIER3_OPS)],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def policy_version(self) -> str:
        return "tier-policy-v1"

    def _disk_pressure(self) -> bool:
        try:
            usage = os.statvfs(str(self.root)) if hasattr(os, "statvfs") else None
            if usage is None:
                import shutil
                total, _, free = shutil.disk_usage(self.root)
                return free / total < 0.05
            return (usage.f_bavail * usage.f_frsize) / (
                usage.f_blocks * usage.f_frsize) < 0.05
        except OSError:
            return False

    # -- snapshot ------------------------------------------------------------

    def collect(self, name: str) -> dict[str, Any]:
        """Run one collector; failures degrade to UNKNOWN, never raise."""
        try:
            return dict(self._sources[name]())
        except Exception as exc:
            return {"error": str(exc), "unknown": True}

    def collect_all(self) -> dict[str, dict[str, Any]]:
        return {name: self.collect(name) for name in self._COLLECTORS}

    def snapshot(
        self, *, kind: StateKind = StateKind.VERIFIED
    ) -> GitSnapshot:
        """Build a unified snapshot (§164).

        FAST returns the cached snapshot (invalidated by any event);
        VERIFIED re-reads every source of truth.
        """
        if kind is StateKind.FAST and self._fast_snapshot is not None:
            return self._fast_snapshot
        data = self.collect_all()
        with self._lock:
            self._generation += 1
            generation = self._generation
        snap = self._build_snapshot(generation, kind, data)
        snap.global_state = self.evaluate_global_state(snap).value
        self._edge_trigger(snap)
        self._update_alerts(snap)
        if kind is StateKind.VERIFIED:
            self._persist(snap)
        self._fast_snapshot = snap
        return snap

    def _build_snapshot(
        self, generation: int, kind: StateKind,
        data: dict[str, dict[str, Any]],
    ) -> GitSnapshot:
        state = data["state"]
        workers = data["workers"]
        queue = data["queue"]
        locks = data["locks"]
        audit = data["audit"]
        remotes = data["remotes"]
        recovery = data["recovery"]
        perf = data["performance"]
        health_extra = data["health"]
        snap = GitSnapshot(
            generation=generation,
            timestamp=self._now(),
            state_kind=kind.value,
            global_state=GitGlobalState.ERROR.value,
            repository_mode=state.get("repository_mode", "worktree"),
            main_revision=state.get("main_revision", ""),
            origin_revision=remotes.get("origin_revision", ""),
            main_dirty=bool(state.get("main_dirty")),
            worktree_count=int(state.get("worktree_count", 0)),
            worker_count=int(workers.get("worker_count", 0)),
            active_workers=int(workers.get("active_workers", 0)),
            dirty_workers=int(workers.get("dirty_workers", 0)),
            quarantined_workers=int(workers.get("quarantined_workers", 0)),
            watcher_count=int(workers.get("watcher_count", 0)),
            duplicate_watchers=int(workers.get("duplicate_watchers", 0)),
            stale_watchers=int(workers.get("stale_watchers", 0)),
            queue_depth=int(queue.get("queue_depth", 0)),
            running_merge=queue.get("running_merge", ""),
            oldest_queue_age=float(queue.get("oldest_queue_age", 0.0)),
            active_locks=int(locks.get("active_locks", 0)),
            stale_locks=int(locks.get("stale_locks", 0)),
            audit_sequence=int(audit.get("audit_sequence", 0)),
            audit_chain_valid=bool(audit.get("audit_chain_valid", True)),
            audit_latency_ms=float(audit.get("audit_latency_ms", 0.0)),
            hook_health=(
                HealthStatus.UNKNOWN.value
                if health_extra.get("unknown") else HealthStatus.HEALTHY.value
            ),
            repository_object_health=state.get(
                "repository_object_health", HealthStatus.UNKNOWN.value),
            disk_pressure=bool(health_extra.get("disk_pressure")),
            remote_relations=dict(remotes.get("remote_relations", {})),
            performance_summary=dict(perf.get("performance_summary", {})),
        )
        snap.health = self._evaluate_dimensions(snap, data)
        snap.reason_codes = sorted({
            rc for d in snap.health.values() for rc in d["reason_codes"]
            if rc != ReasonCode.OK.value
        })
        return snap

    # -- dimensional health (§170-172) ---------------------------------------

    def _evaluate_dimensions(
        self, snap: GitSnapshot, data: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        dims: list[DimensionHealth] = []
        state, workers = data["state"], data["workers"]
        queue, locks, audit = data["queue"], data["locks"], data["audit"]
        remotes, recovery = data["remotes"], data["recovery"]
        perf = data["performance"]

        def _add(dim: Dimension, status: HealthStatus,
                 codes: list[ReasonCode], detail: str = "") -> None:
            dims.append(DimensionHealth(
                dim, status, [c.value for c in codes], detail))

        if state.get("unknown"):
            _add(Dimension.REPOSITORY, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.repository_object_health == HealthStatus.ERROR.value:
            _add(Dimension.REPOSITORY, HealthStatus.ERROR,
                 [ReasonCode.OBJECT_CORRUPTION])
        elif state.get("unknown_main_ref"):
            _add(Dimension.REPOSITORY, HealthStatus.ERROR,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.main_dirty:
            _add(Dimension.REPOSITORY, HealthStatus.DEGRADED,
                 [ReasonCode.MAIN_DIRTY])
        else:
            _add(Dimension.REPOSITORY, HealthStatus.HEALTHY, [ReasonCode.OK])

        _add(Dimension.WORKTREES, HealthStatus.HEALTHY, [ReasonCode.OK])

        if workers.get("unknown"):
            _add(Dimension.WORKERS, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.dirty_workers:
            codes = [ReasonCode.WORKER_DIRTY]
            _add(Dimension.WORKERS, HealthStatus.WARN, codes)
        else:
            _add(Dimension.WORKERS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.duplicate_watchers:
            _add(Dimension.WATCHERS, HealthStatus.DEGRADED,
                 [ReasonCode.DUPLICATE_WATCHER])
        elif snap.stale_watchers:
            _add(Dimension.WATCHERS, HealthStatus.WARN,
                 [ReasonCode.STALE_WATCHER])
        else:
            _add(Dimension.WATCHERS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.queue_depth >= BACKPRESSURE_QUEUE_DEPTH:
            _add(Dimension.QUEUE, HealthStatus.DEGRADED,
                 [ReasonCode.QUEUE_DEPTH_HIGH])
        elif snap.queue_depth:
            _add(Dimension.QUEUE, HealthStatus.WARN, [ReasonCode.OK])
        else:
            _add(Dimension.QUEUE, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.stale_locks:
            _add(Dimension.LOCKS, HealthStatus.DEGRADED,
                 [ReasonCode.STALE_LOCK])
        else:
            _add(Dimension.LOCKS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if audit.get("unknown"):
            _add(Dimension.AUDIT, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif not snap.audit_chain_valid:
            _add(Dimension.AUDIT, HealthStatus.ERROR,
                 [ReasonCode.AUDIT_CHAIN_INVALID])
        elif snap.audit_latency_ms > AUDIT_LATENCY_WARN_MS:
            _add(Dimension.AUDIT, HealthStatus.WARN,
                 [ReasonCode.AUDIT_LATENCY_HIGH])
        else:
            _add(Dimension.AUDIT, HealthStatus.HEALTHY, [ReasonCode.OK])

        hook_codes: list[ReasonCode] = [ReasonCode.OK]
        hook_status = HealthStatus.HEALTHY
        if snap.hook_health in (HealthStatus.ERROR.value,
                                HealthStatus.UNKNOWN.value):
            hook_status = HealthStatus.ERROR
            hook_codes = [ReasonCode.HOOK_HASH_MISMATCH]
        _add(Dimension.HOOKS, hook_status, hook_codes)

        relations = remotes.get("remote_relations", {})
        lvo = relations.get("local_vs_origin", "")
        if remotes.get("unknown"):
            _add(Dimension.ORIGIN, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        else:
            if lvo == "diverged":
                _add(Dimension.ORIGIN, HealthStatus.DEGRADED,
                     [ReasonCode.ORIGIN_DIVERGED])
            else:
                _add(Dimension.ORIGIN, HealthStatus.HEALTHY, [ReasonCode.OK])

        if recovery.get("recovery_in_progress"):
            _add(Dimension.RECOVERY, HealthStatus.DEGRADED,
                 [ReasonCode.RECOVERY_IN_PROGRESS])
        else:
            _add(Dimension.RECOVERY, HealthStatus.HEALTHY, [ReasonCode.OK])

        if perf.get("unknown"):
            _add(Dimension.PERFORMANCE, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        else:
            _add(Dimension.PERFORMANCE, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.disk_pressure:
            _add(Dimension.STORAGE, HealthStatus.DEGRADED,
                 [ReasonCode.DISK_PRESSURE])
        else:
            _add(Dimension.STORAGE, HealthStatus.HEALTHY, [ReasonCode.OK])

        return {d.dimension.value: d.to_dict() for d in dims}

    # -- global state (§163, deterministic) -----------------------------------

    def evaluate_global_state(self, snap: GitSnapshot) -> GitGlobalState:
        health = snap.health
        statuses = {d: h["status"] for d, h in health.items()}
        critical_unknown = any(
            statuses.get(d) == HealthStatus.UNKNOWN.value
            for d in (Dimension.REPOSITORY.value, Dimension.AUDIT.value,
                      Dimension.HOOKS.value, Dimension.ORIGIN.value,
                      Dimension.RECOVERY.value)
        )
        if statuses.get(Dimension.REPOSITORY.value) == HealthStatus.ERROR.value:
            return GitGlobalState.ERROR if any(
                h.get("error") for h in ()
            ) else GitGlobalState.READ_ONLY
        if (statuses.get(Dimension.AUDIT.value) == HealthStatus.ERROR.value
                or statuses.get(Dimension.HOOKS.value) == HealthStatus.ERROR.value):
            return GitGlobalState.READ_ONLY
        if statuses.get(Dimension.RECOVERY.value) == HealthStatus.DEGRADED.value:
            return GitGlobalState.RECOVERY
        if critical_unknown:
            return GitGlobalState.DEGRADED
        degraded = (
            snap.main_dirty
            or statuses.get(Dimension.WATCHERS.value) == HealthStatus.DEGRADED.value
            or statuses.get(Dimension.ORIGIN.value) == HealthStatus.DEGRADED.value
            or statuses.get(Dimension.LOCKS.value) == HealthStatus.DEGRADED.value
        )
        if degraded:
            return GitGlobalState.DEGRADED
        if (snap.queue_depth >= BACKPRESSURE_QUEUE_DEPTH or snap.disk_pressure
                or snap.audit_latency_ms > AUDIT_LATENCY_WARN_MS * 4
                or statuses.get(Dimension.STORAGE.value) == HealthStatus.DEGRADED.value):
            return GitGlobalState.BACKPRESSURE
        if snap.queue_depth or snap.running_merge or (
                snap.worker_count and
                snap.active_workers >= snap.worker_count * BUSY_ACTIVE_WORKER_FRACTION):
            return GitGlobalState.BUSY
        if all(s == HealthStatus.HEALTHY.value for s in statuses.values()):
            return GitGlobalState.HEALTHY
        return GitGlobalState.BUSY

    # -- edge-triggered events + alert dedup (§186-188) ------------------------

    def _edge_trigger(self, snap: GitSnapshot) -> None:
        for dim, h in snap.health.items():
            prev = self._prev_dimension_status.get(dim)
            cur = h["status"]
            if prev is not None and prev != cur:
                severity = _severity_for_status(cur)
                self.emit(GitEvent(
                    EventType.STATE_CHANGED, severity,
                    detail=f"{dim}:{prev}->{cur}",
                ))
                if cur == HealthStatus.HEALTHY.value and prev in (
                        HealthStatus.WARN.value, HealthStatus.DEGRADED.value,
                        HealthStatus.ERROR.value):
                    self.emit(GitEvent(
                        EventType.HEALTH_RECOVERED, Severity.INFO,
                        detail=f"{dim}:{prev}->{cur}",
                    ))
            self._prev_dimension_status[dim] = cur

    def _update_alerts(self, snap: GitSnapshot) -> None:
        now = self._now()
        active = set(snap.reason_codes)
        for code in active:
            entry = self._alerts.get(code)
            if entry is None:
                self._alerts[code] = {
                    "alert_key": code, "first_seen": now,
                    "last_seen": now, "occurrence_count": 1,
                    "state": "ACTIVE",
                }
            else:
                entry["last_seen"] = now
                entry["occurrence_count"] += 1
                entry["state"] = "ACTIVE"
        for code, entry in self._alerts.items():
            if code not in active and entry["state"] == "ACTIVE":
                entry["state"] = "RECOVERED"
                self.emit(GitEvent(
                    EventType.HEALTH_RECOVERED, Severity.INFO,
                    detail=f"alert recovered: {code}",
                ))

    def alerts(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._alerts.items()}

    # -- proposals (§173-175) -------------------------------------------------

    def build_action_proposals(
        self, snap: Optional[GitSnapshot] = None,
    ) -> list[ActionProposal]:
        snap = snap or self.snapshot(kind=StateKind.VERIFIED)
        proposals: list[ActionProposal] = []
        for code in snap.reason_codes:
            spec = _PROPOSAL_SPECS.get(code)
            if spec is None:
                continue
            proposals.append(self._new_proposal(
                snap, reason_code=code, **spec))
        for p in proposals:
            self._proposals[p.proposal_id] = p
            self.emit(GitEvent(
                EventType.PROPOSAL_CREATED, Severity.INFO,
                detail=f"{p.proposal_id}:{p.reason_code}",
            ))
        return proposals

    def _new_proposal(
        self, snap: GitSnapshot, *, reason_code: str, description: str,
        command_plan: list[str], risk_tier: int,
        affected_refs: Optional[list[str]] = None,
        affected_worktrees: Optional[list[str]] = None,
    ) -> ActionProposal:
        return ActionProposal(
            proposal_id=uuid.uuid4().hex[:16],
            created_at=self._now(),
            based_on_generation=snap.generation,
            reason_code=reason_code,
            description=description,
            command_plan=command_plan,
            risk_tier=risk_tier,
            required_approval=(
                "authority" if risk_tier >= 3
                else "confirmation" if risk_tier == 2 else "none"),
            affected_worktrees=affected_worktrees or [],
            affected_refs=affected_refs or [],
            expires_at=self._now() + self._proposal_ttl,
            tier_policy_version=self.policy_version(),
            tier_policy_digest=self.policy_digest(),
            hook_digest_set=self.hook_digest(),
        )

    def validate_proposal(
        self, proposal: ActionProposal, *, generation: Optional[int] = None,
        policy_digest: Optional[str] = None,
        hook_digest: Optional[str] = None,
    ) -> str:
        """Re-verify a proposal against current state before any use.

        Returns "OK" or a machine-readable rejection:
        EXPIRED / STALE_DECISION / POLICY_CHANGED / HOOK_CHANGED.
        """
        if self._now() > proposal.expires_at:
            return "EXPIRED"
        current_gen = generation if generation is not None else self._generation
        if proposal.based_on_generation != current_gen:
            return "STALE_DECISION"
        if (policy_digest if policy_digest is not None
                else self.policy_digest()) != proposal.tier_policy_digest:
            return "POLICY_CHANGED"
        if (hook_digest if hook_digest is not None
                else self.hook_digest()) != proposal.hook_digest_set:
            return "HOOK_CHANGED"
        return "OK"

    # -- command API (§198): validate + record, never execute ----------------

    def request_action(
        self,
        proposal: ActionProposal,
        *,
        capability: Optional[CapabilityToken] = None,
        legacy_env_approval: bool = False,
        actor: str = "unknown",
    ) -> dict[str, Any]:
        """Submit a proposal for governance — the control plane never
        executes Git commands (§162, §174, §198)."""
        verdict = self.validate_proposal(proposal)
        approval_path = (
            "LEGACY_APPROVAL_PATH" if legacy_env_approval else "proposal"
        )
        decision = {
            "snapshot_generation": proposal.based_on_generation,
            "reason_codes": [proposal.reason_code],
            "proposal": proposal.to_dict(),
            "approval": approval_path,
            "actor": actor,
            "validation": verdict,
            "execution_result": "NOT_EXECUTED",
        }
        if verdict != "OK":
            decision["status"] = f"REJECTED:{verdict}"
        elif capability is not None:
            consumed = self.consume_capability(capability, proposal)
            decision["status"] = (
                "PROPOSAL_AUTHORIZED" if consumed
                else "REJECTED:CAPABILITY_INVALID")
        elif proposal.is_tier3():
            # §174: never auto-execute, never auto-escalate.
            decision["status"] = "PROPOSAL_PENDING_AUTHORITY"
        else:
            decision["status"] = "PROPOSAL_PENDING_APPROVAL"
        self._decisions.append(decision)
        return decision

    def decision_log(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._decisions]

    # -- capabilities (§199-201) ----------------------------------------------

    def issue_capability(
        self, *, actor: str, operation: str, proposal: ActionProposal,
        worktree: str = "", ref: str = "", ttl: float = 300.0,
    ) -> CapabilityToken:
        token = CapabilityToken(
            token_id=uuid.uuid4().hex,
            actor=actor, operation=operation,
            repository=str(self.root), worktree=worktree, ref=ref,
            proposal_id=proposal.proposal_id,
            generation=proposal.based_on_generation,
            expires_at=self._now() + ttl,
            nonce=uuid.uuid4().hex,
        )
        self._capabilities[token.token_id] = token
        return token

    def consume_capability(
        self, token: CapabilityToken, proposal: ActionProposal,
        *, result: str = "consumed",
    ) -> bool:
        """Single-use: bound to actor+operation+proposal+generation (§200)."""
        stored = self._capabilities.get(token.token_id)
        if stored is None or stored.used_at:
            return False
        if self._now() > stored.expires_at:
            return False
        if (stored.proposal_id != proposal.proposal_id
                or stored.generation != proposal.based_on_generation
                or stored.actor != token.actor
                or stored.operation != token.operation):
            return False
        stored.used_at = self._now()
        stored.result = result
        return True

    # -- command correlation + transaction trace (§195-196) -------------------

    def new_command_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def record_command(
        self, *, command_id: str, command: str, task_id: str = "",
        worker_id: str = "", transaction_id: str = "",
        audit_sequence: int = 0,
    ) -> None:
        self._commands.append({
            "command_id": command_id, "command": command,
            "task_id": task_id, "worker_id": worker_id,
            "transaction_id": transaction_id,
            "audit_sequence": audit_sequence,
            "timestamp": self._now(),
        })

    def trace_transaction(self, transaction_id: str) -> dict[str, Any]:
        """§196 — full timeline: events + commands for one transaction."""
        return {
            "transaction_id": transaction_id,
            "events": [e.to_dict() for e in self.events(
                transaction_id=transaction_id)],
            "commands": [c for c in self._commands
                         if c["transaction_id"] == transaction_id],
        }

    # -- progress heartbeat + deadlock watchdog (§193-194) ---------------------

    def record_heartbeat(self, operation_id: str, phase: str) -> None:
        entry = self._heartbeats.setdefault(operation_id, {
            "operation_id": operation_id, "started_at": self._now(),
            "phases": [],
        })
        entry["phases"].append({"phase": phase, "at": self._now()})
        entry["last_seen"] = self._now()

    def deadlock_watchdog(self) -> list[dict[str, Any]]:
        """Detect suspected stalls only — never kills a process (§193)."""
        warnings: list[dict[str, Any]] = []
        locks = self.collect("locks").get("locks", [])
        for lock in locks:
            if lock.get("stale"):
                warnings.append({
                    "kind": "LOCK_STALL_WARNING",
                    "lock": lock["name"],
                    "age_seconds": lock["age_seconds"],
                })
                self.emit(GitEvent(
                    EventType.LOCK_STALL_WARNING, Severity.WARN,
                    detail=f"lock stall: {lock['name']}",
                ))
        return warnings

    # -- supervisor read-only API (§197) ---------------------------------------

    def get_global_state(self) -> dict[str, Any]:
        snap = self.snapshot(kind=StateKind.FAST) or self.snapshot()
        return {
            "global_state": snap.global_state,
            "generation": snap.generation,
            "reason_codes": snap.reason_codes,
            "health": snap.health,
        }

    def get_worker_state(self) -> dict[str, Any]:
        return self.collect("workers")

    def get_queue_state(self) -> dict[str, Any]:
        return self.collect("queue")

    def get_remote_state(self) -> dict[str, Any]:
        return self.collect("remotes")

    def get_audit_health(self) -> dict[str, Any]:
        return self.collect("audit")

    def get_recovery_health(self) -> dict[str, Any]:
        return self.collect("recovery")

    def get_performance_health(self) -> dict[str, Any]:
        return self.collect("performance")

    # -- persistence + restart reconcile (§206-207) -----------------------------

    def _persist(self, snap: GitSnapshot) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_verified_snapshot": snap.to_dict(),
                "last_generation": snap.generation,
                "open_incidents": [
                    k for k, a in self._alerts.items()
                    if a["state"] == "ACTIVE"],
                "proposals": [p.to_dict() for p in self._proposals.values()],
            }
            tmp = self._state_dir / "state.json.tmp"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._state_dir / "state.json")
        except OSError:
            pass

    def restart_reconcile(self) -> dict[str, Any]:
        """§207 — reload persisted state, then let verified Git truth win."""
        state_path = self._state_dir / "state.json"
        persisted: dict[str, Any] = {}
        try:
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            persisted = {}
        live = self.snapshot(kind=StateKind.VERIFIED)
        last = persisted.get("last_verified_snapshot") or {}
        diverged = bool(last) and (
            last.get("main_revision") != live.main_revision
            or persisted.get("last_generation") != live.generation
        )
        self._generation = max(self._generation,
                               int(persisted.get("last_generation", 0)))
        return {
            "persisted_found": bool(persisted),
            "status": "STALE_RECONCILED" if diverged else "RECONCILED",
            "verified_generation": live.generation,
        }


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
    "EventType", "GitControlPlane", "GitEvent", "GitGlobalState",
    "GitSnapshot", "HealthStatus", "ReasonCode", "Severity", "StateKind",
]
