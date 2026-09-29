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

Module layout (A185 source-size split):

    worker_pool_support.py  constants + governed command/config/dir helpers
    worker_pool_alloc.py    AllocationMixin — §40 allocation + binding
    worker_lifecycle.py     LifecycleMixin — lease/quarantine/retire/recycle
    worker_reconcile.py     ReconcileMixin — startup reconciliation
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from .branch_policy import MAIN_BRANCH
from .git_repository import GitRepository
from .merge_queue import MergeQueue
from .paths import PERSISTENT_WORKTREES_DIRNAME, contained
from .process_lock import ProcessFileLock
from .worker_lifecycle import LifecycleMixin
from .worker_pool_alloc import AllocationMixin
from .worker_pool_support import (  # noqa: F401  (re-exported helpers)
    CONFIG_FILE,
    CONFLICT_STATS_FILE,
    WORKER_POOL_ACTOR,
    _governed_worker_command,
    _now,
    _pool_dir,
    _RETIREMENT_LEGACY_APPROVAL,
    load_pool_config,
)
from .worker_pool_types import (
    AllocationResult, PoolConfig, PoolHealth, PoolType,
    WorkerSlot, WorkerState, can_transition, new_instance_id,
)
from .worker_reconcile import ReconcileMixin
from .worker_registry import PoolRegistry

PERSISTENT_ROOT_NAME = PERSISTENT_WORKTREES_DIRNAME


class WorkerPool(AllocationMixin, LifecycleMixin, ReconcileMixin):
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
            path = contained(
                self.root,
                Path(PERSISTENT_ROOT_NAME) / name,
                purpose="persistent-worktree",
            )
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
        origin_healthy: bool,
    ) -> bool:
        """§62: stop pool growth on pressure — workers keep working."""
        active = (
            queue_depth >= self.config.backpressure_queue_depth
            or disk_free_fraction <= self.config.backpressure_free_fraction
            or not main_healthy
            or not origin_healthy
        )
        self.set_backpressure(
            active,
            reason=(
                f"queue={queue_depth} disk={disk_free_fraction:.2f} "
                f"main={main_healthy} origin={origin_healthy}"
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
