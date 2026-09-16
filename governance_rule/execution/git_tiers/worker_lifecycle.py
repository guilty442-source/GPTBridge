"""Worker lifecycle operations — lease, quarantine, retire, recycle,
stale classification (task §43/§44/§45/§50/§51/§52/§53).

Mixin for ``WorkerPool``; separated per A426 file-size governance.

Hard rules honoured here:
    * lease expiry never drops dirty work — dirty -> QUARANTINED
    * quarantined workers get no assignment / merge / auto-retire
    * retirement is verification-gated; branches are never deleted,
      merged branches enter MERGED_RETAINED retention
    * persistent workers never auto-retire (§53)
    * recycle never uses ``reset --hard``
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .audit_chain import chained_audit_log
from .branch_policy import normalize_branch
from .merge_queue import MergeQueue
from .process_lock import ProcessFileLock
from .worker_pool_types import (
    PoolType, ReconcileClass, WorkerSlot, WorkerState, can_transition,
)


def _now() -> float:
    return time.time()


class LifecycleMixin:
    """Lease/quarantine/retire/recycle/stale operations.

    Expects the host class to provide: ``config``, ``registry``,
    ``_slots()``, ``_save()``, ``_lock_path``, ``_worktree_dirty()``,
    ``_remove_worktree()``, ``_force_state()``, ``repo``, ``root``.
    """

    # -- lease (§44) -------------------------------------------------------

    def renew_lease(self, worker_id: str, instance_id: str) -> bool:
        """Heartbeat: extend the lease only for the true owner."""
        slots = self._slots()
        slot = slots.get(worker_id)
        if slot is None or slot.instance_id != instance_id:
            return False
        slot.lease_expires_at = _now() + self.config.worker_lease_seconds
        slot.last_activity = _now()
        self._save(slots)
        return True

    def check_leases(self, *, now: Optional[float] = None) -> list[str]:
        """Expired leases reconcile, never vanish (§44):

            dirty      -> QUARANTINED
            clean+idle -> RETIRING
        """
        now = now if now is not None else _now()
        expired: list[str] = []
        slots = self._slots()
        for slot in slots.values():
            if slot.pool_type is not PoolType.EPHEMERAL:
                continue
            if not slot.lease_expires_at or slot.lease_expires_at > now:
                continue
            if slot.state in (
                WorkerState.FREE, WorkerState.RETIRING,
                WorkerState.QUARANTINED,
            ):
                continue
            expired.append(slot.worker_id)
            if self._worktree_dirty(slot):
                self._force_state(slots, slot, WorkerState.QUARANTINED)
            else:
                self._force_state(slots, slot, WorkerState.RETIRING)
        if expired:
            self._save(slots)
        return expired

    def quarantine(self, worker_id: str, reason: str) -> bool:
        """§45: suspicious workers leave the reusable set entirely —
        no new assignment, no auto merge, no auto retire."""
        slots = self._slots()
        slot = slots.get(worker_id)
        if slot is None:
            return False
        self._force_state(slots, slot, WorkerState.QUARANTINED)
        slot.display_name = f"quarantined:{reason}"
        self._save(slots)
        chained_audit_log(
            2, "worker-pool quarantine", "governance/automation-supervisor",
            True, f"{worker_id} {reason}",
            operation="worker-pool", phase="result", result="quarantined",
        )
        return True

    # -- retire / recycle (§43/§52/§53) -----------------------------------

    def retire_worker(
        self, worker_id: str, *, merged: bool = False
    ) -> bool:
        """Verification-gated retirement (§52).  The branch is never
        deleted — merged branches enter MERGED_RETAINED retention."""
        with ProcessFileLock(self._lock_path):
            slots = self._slots()
            slot = slots.get(worker_id)
            if slot is None or slot.pool_type is not PoolType.EPHEMERAL:
                return False
            self._force_state(slots, slot, WorkerState.RETIRING)
            if merged:
                slot.merged_retained_until = (
                    _now() + self.config.merged_branch_retention_seconds
                )
            slot.retired_at = _now()
            if self._worktree_dirty(slot):
                self._force_state(slots, slot, WorkerState.QUARANTINED)
                self._save(slots)
                return False
            self._remove_worktree(slot)
            slot.state = WorkerState.FREE
            slot.task_id = ""
            slot.branch = ""
            slot.queue_id = ""
            slot.watcher_pid = 0
            slot.lease_expires_at = 0.0
            self._save(slots)
            return True

    def recycle_worker(
        self, worker_id: str, *, task_id: str, domain: str = "",
        base_branch: str = "main",
    ) -> bool:
        """§43 warm recycle: verify clean -> rebind branch -> READY.
        Unsafe slots quarantine instead of ``reset --hard``."""
        with ProcessFileLock(self._lock_path):
            slots = self._slots()
            slot = slots.get(worker_id)
            if slot is None or slot.pool_type is not PoolType.EPHEMERAL:
                return False
            if self._worktree_dirty(slot):
                self._force_state(slots, slot, WorkerState.QUARANTINED)
                self._save(slots)
                return False
            self._force_state(slots, slot, WorkerState.RECYCLING)
            slot.task_id = task_id
            slot.domain = domain or slot.domain
            slot.base_branch = base_branch
            try:
                self._bind_git(slot)
            except Exception:
                self._force_state(slots, slot, WorkerState.QUARANTINED)
                self._save(slots)
                return False
            slot.state = WorkerState.READY
            slot.allocated_at = _now()
            slot.lease_expires_at = _now() + self.config.worker_lease_seconds
            self._save(slots)
            return True

    def release_worker(self, worker_id: str, instance_id: str = "") -> bool:
        """Release a finished task binding; FREE only when clean —
        dirty releases quarantine (§44/§45)."""
        slots = self._slots()
        slot = slots.get(worker_id)
        if slot is None or slot.pool_type is not PoolType.EPHEMERAL:
            return False
        if instance_id and slot.instance_id != instance_id:
            return False
        if self._worktree_dirty(slot):
            self._force_state(slots, slot, WorkerState.QUARANTINED)
        else:
            self._force_state(slots, slot, WorkerState.FREE)
            slot.task_id = ""
            slot.branch = ""
            slot.lease_expires_at = 0.0
        self._save(slots)
        return True

    # -- stale classification (§51) ----------------------------------------

    def classify_stale(
        self,
        worker_id: str,
        *,
        ai_pid_alive: bool,
        watcher_alive: bool,
        queued: bool,
        now: Optional[float] = None,
    ) -> ReconcileClass:
        """Composite stale check — never time-only (§51)."""
        now = now if now is not None else _now()
        slot = self._slots().get(worker_id)
        if slot is None:
            return ReconcileClass.ORPHAN
        if slot.state is WorkerState.QUARANTINED:
            return ReconcileClass.QUARANTINED
        lease_expired = bool(
            slot.lease_expires_at and slot.lease_expires_at <= now
        )
        dirty = self._worktree_dirty(slot)
        if not ai_pid_alive and dirty:
            return ReconcileClass.QUARANTINED  # STALE_DIRTY
        if not ai_pid_alive and not dirty and not queued and lease_expired:
            return ReconcileClass.STALE  # STALE_CLEAN
        return ReconcileClass.VALID

    def check_base_staleness(
        self, worker_id: str, *, max_lag_commits: int = 30
    ) -> Optional[str]:
        """§50: detect a worker building on an old base.  Reports
        ``RESYNC_REQUIRED`` — never auto-rebases."""
        slot = self._slots().get(worker_id)
        if slot is None or not slot.base_revision:
            return None
        result = self.repo.run([
            "rev-list", "--count",
            f"{slot.base_revision}..{normalize_branch(slot.base_branch)}",
        ])
        try:
            lag = int((result.stdout or "0").strip())
        except ValueError:
            return None
        return "RESYNC_REQUIRED" if lag >= max_lag_commits else None


__all__ = ["LifecycleMixin"]
