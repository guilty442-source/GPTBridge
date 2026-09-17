"""Dynamic Git Worker Pool (task §37–§72, A375).

One pool, two classes:

    persistent — git / local-model / rag / ui (never auto-retired)
    ephemeral  — supervisor-issued wNNN slots for AI tasks

Lifecycle per §37:

    task -> allocate -> worktree+branch -> AI work -> self-commit
         -> SHA-pinned merge queue -> merge -> sync -> retire/recycle

Hard rules: ``main`` is never a worker; one worker : one worktree :
one branch : one watcher; enum-guarded states only; no force-push,
no reset --hard, no auto branch deletion.

Lifecycle (lease/quarantine/retire/recycle/stale) lives in
``worker_lifecycle.LifecycleMixin`` and startup reconciliation in
``worker_reconcile.ReconcileMixin`` per A426 file-size governance.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .audit_chain import chained_audit_log
from .branch_policy import MAIN_BRANCH, is_ephemeral, normalize_branch
from .git_repository import GitRepository
from .merge_queue import MergeQueue
from .process_lock import ProcessFileLock
from .worker_identity import (
    attempt_id_for, build_branch_name, next_worker_id, valid_git_branch,
    worktree_path_for,
)
from .worker_lifecycle import LifecycleMixin
from .worker_reconcile import ReconcileMixin
from .worker_pool_types import (
    AllocationResult, PoolConfig, PoolHealth, PoolType,
    WorkerSlot, WorkerState, can_transition, new_instance_id,
)
from .worker_registry import PoolRegistry
from .worker_routing import route_task

CONFIG_FILE = "config.json"
CONFLICT_STATS_FILE = "conflict_stats.json"
PERSISTENT_ROOT_NAME = "GPTBridge-worktrees"
WORKERS_ROOT_NAME = "GPTBridge-workers"
WORKER_POOL_ACTOR = "governance/worker-pool"

#: Retirement keeps the documented audited legacy adapter (see
#: ``_remove_worktree``); the boolean is a policy statement, not a literal
#: approval at the gateway.
_RETIREMENT_LEGACY_APPROVAL: bool = True


def _governed_worker_command(repo: GitRepository, args: list[str]):
    """Run one worker-allocation write through the capability gate."""
    from .capability_gate import execute_system_safe

    return execute_system_safe(
        list(args), actor=WORKER_POOL_ACTOR, repo_path=repo.path,
    )


def _pool_dir(root: str | Path) -> Path:
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    common = Path((result.stdout or "").strip())
    if not common.is_absolute():
        common = repo.path / common
    directory = common.resolve() / "gptbridge-automation" / "worker-pool"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_pool_config(root: str | Path) -> PoolConfig:
    """Governed pool config: deployment JSON overrides dataclass
    defaults (A386 — values resolved, not scattered)."""
    path = _pool_dir(root) / CONFIG_FILE
    data: dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    config = PoolConfig()
    for key in PoolConfig.__dataclass_fields__:
        if key in data:
            setattr(config, key, data[key])
    if not config.workers_root:
        config.workers_root = str(
            Path(root).resolve().parent / WORKERS_ROOT_NAME
        )
    return config


def _now() -> float:
    return time.time()


class WorkerPool(LifecycleMixin, ReconcileMixin):
    """Dynamic pool orchestrator bound to one repository."""

    def __init__(
        self,
        root: str | Path,
        *,
        config: Optional[PoolConfig] = None,
        spawn_watcher: Optional[Callable[[WorkerSlot], int]] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.repo = GitRepository(self.root)
        self._dir = _pool_dir(self.root)
        self.config = config or load_pool_config(self.root)
        self.registry = PoolRegistry(self._dir)
        self._lock_path = self._dir / "worker-pool.lock"
        self._spawn_watcher = spawn_watcher or (lambda slot: 0)
        self._ensure_persistent_slots()

    # -- registry helpers ------------------------------------------------

    def _slots(self) -> dict[str, WorkerSlot]:
        return self.registry.load_slots()

    def _save(self, slots: dict[str, WorkerSlot], **extra: Any) -> None:
        self.registry.store_slots(slots, extra=extra or None)

    def _ensure_persistent_slots(self) -> None:
        slots = self._slots()
        changed = False
        for name in self.config.persistent_workers:
            if name in slots:
                continue
            path = self.root.parent / PERSISTENT_ROOT_NAME / name
            slots[name] = WorkerSlot(
                worker_id=name,
                instance_id=new_instance_id(),
                pool_type=PoolType.PERSISTENT,
                state=WorkerState.READY,
                worktree_path=str(path),
                branch=name,
                base_branch=MAIN_BRANCH,
                domain=name,
                created_at=_now(),
                last_activity=_now(),
            )
            changed = True
        if changed:
            try:
                self._save(slots)
            except Exception:
                pass

    def list_workers(
        self, pool_type: Optional[PoolType] = None
    ) -> list[WorkerSlot]:
        slots = self._slots().values()
        if pool_type is None:
            return list(slots)
        return [s for s in slots if s.pool_type is pool_type]

    # -- capacity --------------------------------------------------------

    def available_capacity(self) -> dict[str, Any]:
        slots = self._slots()
        ephemeral = [
            s for s in slots.values() if s.pool_type is PoolType.EPHEMERAL
        ]
        active = [
            s for s in ephemeral
            if s.state not in (WorkerState.FREE, WorkerState.RETIRING)
        ]
        free = [s for s in ephemeral if s.state is WorkerState.FREE]
        return {
            "ephemeral_active": len(active),
            "ephemeral_free": len(free),
            "ephemeral_capacity": self.config.max_ephemeral_workers,
            "ephemeral_available": max(
                0, self.config.max_ephemeral_workers - len(active)
            ),
            "total_workers": len(slots),
            "total_capacity": self.config.max_total_workers,
            "persistent": len(self.config.persistent_workers),
        }

    def _storage_limit_hit(self) -> bool:
        """§58: stop new workers when worktree disk usage exceeds the
        limit — never prune an active worktree to make room."""
        root = Path(self.config.workers_root)
        if not root.is_dir():
            return False
        total = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
        return total >= self.config.worktree_storage_limit_bytes

    # -- allocation (§40) ------------------------------------------------

    def allocate_worker(
        self,
        task_id: str,
        *,
        paths: tuple[str, ...] = (),
        domain: str = "",
        base_branch: str = MAIN_BRANCH,
        attempt: int = 1,
        priority: int = 0,
        actor: str = "governance/automation-supervisor",
        parent_task_id: str = "",
    ) -> tuple[AllocationResult, Optional[WorkerSlot]]:
        """Allocate a slot for a task (12-step flow, §40).

        A slot is handed to the AI only after every step succeeds —
        partial allocations roll back to FAILED, never half-bound.
        """
        if not task_id:
            return AllocationResult.DENIED, None
        payload = self.registry.load()
        if payload.get("pool_state") == "POOL_DRAINING":
            return AllocationResult.POOL_DRAINING, None
        if payload.get("backpressure"):
            return AllocationResult.BACKPRESSURE, None
        if self._storage_limit_hit():
            return AllocationResult.POOL_CAPACITY_STORAGE_LIMIT, None

        domain = domain or route_task(list(paths))
        if base_branch == MAIN_BRANCH and domain in self.config.persistent_workers:
            base_branch = domain  # domain-integration queue (§55)

        with ProcessFileLock(self._lock_path):
            slots = self._slots()
            slot = self._pick_free(slots)
            if slot is None:
                capacity = self.available_capacity()
                if capacity["ephemeral_available"] <= 0 or (
                    capacity["total_workers"] >= self.config.max_total_workers
                ):
                    return AllocationResult.WAITING_FOR_WORKER, None
                slot = self._new_ephemeral_slot(slots)
                slots[slot.worker_id] = slot
            slot.state = WorkerState.ALLOCATING
            slot.task_id = task_id
            slot.attempt_id = attempt_id_for(task_id, attempt)
            slot.domain = domain
            slot.base_branch = base_branch
            slot.priority = priority
            slot.parent_task_id = parent_task_id
            slot.allocated_at = _now()
            slot.last_activity = _now()
            slot.lease_expires_at = _now() + self.config.worker_lease_seconds
            slot.instance_id = new_instance_id()
            try:
                self._bind_git(slot)
            except Exception as exc:
                slot.state = WorkerState.FAILED
                slot.display_name = f"alloc-failed:{exc}"
                self._save(slots)
                return AllocationResult.DENIED, None
            slot.state = WorkerState.READY
            slot.watcher_pid = self._spawn_watcher(slot)
            self._save(slots)
        chained_audit_log(
            2, "worker-pool allocate", actor, True,
            f"{slot.worker_id}:{slot.instance_id} {slot.branch}",
            operation="worker-pool", phase="result", result="allocated",
        )
        return AllocationResult.ALLOCATED, slot

    def _pick_free(self, slots: dict[str, WorkerSlot]) -> Optional[WorkerSlot]:
        candidates = [
            s for s in slots.values()
            if s.pool_type is PoolType.EPHEMERAL
            and s.state is WorkerState.FREE
        ]
        return min(candidates, key=lambda s: s.worker_id, default=None)

    def _new_ephemeral_slot(self, slots: dict[str, WorkerSlot]) -> WorkerSlot:
        worker_id = next_worker_id(list(slots))
        return WorkerSlot(
            worker_id=worker_id,
            instance_id=new_instance_id(),
            pool_type=PoolType.EPHEMERAL,
            state=WorkerState.FREE,
            worktree_path=worktree_path_for(
                self.config.workers_root, worker_id
            ),
            created_at=_now(),
        )

    def _bind_git(self, slot: WorkerSlot) -> None:
        """Steps 5–9 of allocation: base revision, branch, worktree,
        mapping verification — all through the governed entrypoint."""
        head = self.repo.run(
            ["rev-parse", normalize_branch(slot.base_branch)]
        )
        slot.base_revision = (head.stdout or "").strip()
        if not slot.base_revision:
            raise RuntimeError(f"base-revision-missing:{slot.base_branch}")
        slot.branch = build_branch_name(
            slot.domain, slot.task_id, slot.worker_id
        )
        if not valid_git_branch(slot.branch):
            raise RuntimeError(f"branch-name-invalid:{slot.branch}")
        if not is_ephemeral(slot.branch):
            raise RuntimeError(f"branch-not-ephemeral:{slot.branch}")
        path = Path(slot.worktree_path)
        if not path.exists():
            gate = _governed_worker_command(
                self.repo,
                [
                    "worktree", "add", "-b", slot.branch,
                    str(path), slot.base_revision,
                ],
            )
        else:
            gate = _governed_worker_command(
                self.repo,
                [
                    "-C", str(path), "switch", "-c", slot.branch,
                    slot.base_revision,
                ],
            )
        result = gate.execution_result
        if gate.allowed is False or result is None or result.returncode != 0:
            detail = (
                gate.detail if gate.allowed is False
                else str(getattr(result, "stderr", "")).strip()[:200]
            )
            raise RuntimeError(f"worktree-bind-failed:{detail}")
        slot.head_revision = slot.base_revision
        self._verify_mapping(slot)

    def _verify_mapping(self, slot: WorkerSlot) -> None:
        """One worker : one worktree : one branch — verified, not assumed."""
        result = self.repo.run(
            ["-C", slot.worktree_path, "rev-parse", "--abbrev-ref", "HEAD"]
        )
        actual = (result.stdout or "").strip()
        if actual != slot.branch:
            raise RuntimeError(
                f"mapping-mismatch:{slot.worktree_path}:{actual}"
            )

    # -- state transitions ------------------------------------------------

    def transition(
        self, worker_id: str, target: WorkerState,
        *, instance_id: str = "",
    ) -> bool:
        """Enum-guarded state move; instance binding prevents stale
        owners mutating a recycled slot (§68)."""
        with ProcessFileLock(self._lock_path):
            slots = self._slots()
            slot = slots.get(worker_id)
            if slot is None:
                return False
            if instance_id and slot.instance_id != instance_id:
                return False
            if not can_transition(slot, target):
                return False
            slot.state = target
            slot.last_activity = _now()
            self._save(slots)
            return True

    def mark_activity(self, worker_id: str, instance_id: str = "") -> None:
        slots = self._slots()
        slot = slots.get(worker_id)
        if slot and (not instance_id or slot.instance_id == instance_id):
            slot.last_activity = _now()
            self._save(slots)

    # -- merge handoff ------------------------------------------------------

    def enqueue_merge(
        self, worker_id: str, source_commit: str, *,
        instance_id: str = "",
    ) -> str:
        """Bind the worker's immutable SHA into the governed queue."""
        slots = self._slots()
        slot = slots.get(worker_id)
        if slot is None or slot.pool_type is not PoolType.EPHEMERAL:
            raise KeyError(worker_id)
        if instance_id and slot.instance_id != instance_id:
            raise PermissionError("instance-mismatch")
        queue = MergeQueue(self.root)
        entry = queue.enqueue(
            worker_id, slot.branch, source_commit,
            base_main_commit=slot.base_revision,
            task_id=slot.task_id,
        )
        queue_id = str(entry.get("queue_id", ""))
        if not queue_id:
            raise RuntimeError(f"enqueue-failed:{entry.get('detail')}")
        slot.queue_id = queue_id
        slot.head_revision = source_commit
        if can_transition(slot, WorkerState.QUEUED):
            slot.state = WorkerState.QUEUED
        self._save(slots)
        return queue_id

    # -- backpressure / health (§62/§63) ------------------------------------

    def set_backpressure(self, active: bool, reason: str = "") -> None:
        payload = self.registry.load()
        payload["backpressure"] = bool(active)
        payload["backpressure_reason"] = reason if active else ""
        self.registry.store(payload)

    def evaluate_backpressure(
        self,
        *,
        queue_depth: int,
        disk_free_fraction: float,
        main_healthy: bool,
        central_healthy: bool,
    ) -> bool:
        """§62: stop pool growth on pressure — workers keep working."""
        active = (
            queue_depth >= self.config.backpressure_queue_depth
            or disk_free_fraction <= self.config.backpressure_free_fraction
            or not main_healthy
            or not central_healthy
        )
        self.set_backpressure(
            active,
            reason=(
                f"queue={queue_depth} disk={disk_free_fraction:.2f} "
                f"main={main_healthy} central={central_healthy}"
            ),
        )
        return active

    def worker_health(self) -> dict[str, Any]:
        slots = self._slots()
        counts: dict[str, int] = {}
        for slot in slots.values():
            counts[slot.state.value] = counts.get(slot.state.value, 0) + 1
        capacity = self.available_capacity()
        payload = self.registry.load()
        quarantined = counts.get("QUARANTINED", 0)
        if payload.get("pool_state") == "POOL_DRAINING":
            health = PoolHealth.DRAINING
        elif payload.get("backpressure"):
            health = PoolHealth.BACKPRESSURE
        elif quarantined:
            health = PoolHealth.DEGRADED
        elif capacity["ephemeral_available"] <= 0:
            health = PoolHealth.BUSY
        else:
            health = PoolHealth.HEALTHY
        return {
            "health": health.value,
            "state_counts": counts,
            "capacity": capacity,
            "backpressure": bool(payload.get("backpressure")),
            "waiting_tasks": len(payload.get("waiting", [])),
        }

    # -- shutdown (§65) ------------------------------------------------------

    def begin_drain(self) -> None:
        """Stop allocation; workers finish checkpoints — never deletes
        worktrees on shutdown (§65)."""
        payload = self.registry.load()
        payload["pool_state"] = "POOL_DRAINING"
        self.registry.store(payload)

    def finish_drain(self) -> None:
        payload = self.registry.load()
        payload["pool_state"] = "STOPPED"
        self.registry.store(payload)

    def resume(self) -> None:
        payload = self.registry.load()
        payload["pool_state"] = "RUNNING"
        self.registry.store(payload)

    # -- conflict hotspots (§56) ----------------------------------------------

    def record_conflict_paths(self, paths: list[str]) -> None:
        stats_path = self._dir / CONFLICT_STATS_FILE
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stats = {}
        for path in paths:
            stats[path] = int(stats.get(path, 0)) + 1
        stats_path.write_text(
            json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8"
        )

    def conflict_hotspots(self, threshold: int = 3) -> dict[str, int]:
        stats_path = self._dir / CONFLICT_STATS_FILE
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {p: c for p, c in stats.items() if c >= threshold}

    # -- internals ------------------------------------------------------------

    def _worktree_dirty(self, slot: WorkerSlot) -> bool:
        path = Path(slot.worktree_path)
        if not path.is_dir():
            return False
        result = self.repo.run(
            ["-C", str(path), "status", "--porcelain=v2", "-z"]
        )
        return bool((result.stdout or "").strip("\x00").strip())

    def _remove_worktree(self, slot: WorkerSlot) -> None:
        """Retire a worker worktree through the governed gate.

        ``worktree remove`` is deliberately outside the SYSTEM_SAFE whitelist
        (guarded by ``test_capability.py``), so this last non-whitelisted
        Tier-2 automation step uses the gate's audited legacy adapter: the
        decision is recorded as ``LEGACY_CONFIRM`` marked
        ``DEPRECATED_COMPATIBILITY``, and automation still can never
        self-authorize Tier-3.  Migrate once the capability policy admits
        clean worktree removal.
        """
        from .capability_gate import execute_with_capability

        path = Path(slot.worktree_path)
        if not path.is_dir():
            return
        gate = execute_with_capability(
            ["worktree", "remove", str(path)], None, tier=2,
            actor=WORKER_POOL_ACTOR, repo_path=self.repo.path,
            legacy_confirmed=_RETIREMENT_LEGACY_APPROVAL,
        )
        result = gate.execution_result
        if gate.allowed is False or result is None or result.returncode != 0:
            detail = (
                f"{gate.code}:{gate.detail}" if result is None
                else str(result.stderr or "").strip()[:200]
            )
            raise PermissionError(f"worktree-remove-denied:{detail}")

    @staticmethod
    def _force_state(
        slots: dict[str, WorkerSlot], slot: WorkerSlot, target: WorkerState
    ) -> None:
        """Internal moves (quarantine/retire) bypass the public
        transition table — the table guards *AI-visible* moves."""
        slot.state = target
        slot.last_activity = _now()


__all__ = [
    "CONFIG_FILE",
    "WorkerPool",
    "load_pool_config",
]
