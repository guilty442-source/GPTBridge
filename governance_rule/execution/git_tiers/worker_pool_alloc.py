"""Worker-pool allocation mixin (A185 split).

Extracted from ``worker_pool.py`` (source-size contract): the §40
twelve-step allocation flow — free-slot pick, ephemeral slot creation,
governed worktree/branch binding and the one-worker:one-worktree:
one-branch mapping verification.  Mixed into ``worker_pool.WorkerPool``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .audit_chain import chained_audit_log
from .branch_policy import MAIN_BRANCH, is_ephemeral, normalize_branch
from .paths import contained
from .process_lock import ProcessFileLock
from .worker_identity import (
    attempt_id_for, build_branch_name, next_worker_id, valid_git_branch,
    worktree_path_for,
)
from .worker_pool_support import _governed_worker_command, _now
from .worker_pool_types import (
    AllocationResult, PoolType, WorkerSlot, WorkerState, new_instance_id,
)
from .worker_routing import route_task


class AllocationMixin:
    """§40 allocation flow — partial allocations roll back to FAILED."""

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
        path = contained(self.root, slot.worktree_path, purpose="worker-worktree")
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


__all__ = ["AllocationMixin"]
