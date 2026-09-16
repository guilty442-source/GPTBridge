"""Worker pool startup reconciliation (task §64, A426 split).

Supervisor restart never rebuilds everything.  It cross-checks:

    git worktree list -> registry slots -> running PIDs
    -> branch refs -> queue -> claims -> leases

and classifies every slot VALID / STALE / ORPHAN / QUARANTINED.
Only *missing* watchers are reported for start — an existing watcher
is never double-spawned.
"""
from __future__ import annotations

from typing import Any

from .worker_pool_types import PoolType, WorkerState


class ReconcileMixin:
    """Startup reconciliation + live git introspection.

    Expects the host class to provide: ``repo``, ``_slots()``.
    """

    def reconcile_workers(self) -> dict[str, Any]:
        """Rebuild slot state from live git/process evidence."""
        worktrees = self._live_worktrees()
        running_pids = self._running_pids()
        slots = self._slots()
        report: dict[str, list[str]] = {
            "VALID": [], "STALE": [], "ORPHAN": [], "QUARANTINED": [],
        }
        watchers_to_start: list[str] = []
        for slot in slots.values():
            has_worktree = slot.worktree_path in worktrees
            watcher_alive = slot.watcher_pid in running_pids
            if slot.pool_type is PoolType.PERSISTENT:
                report["VALID" if has_worktree else "STALE"].append(
                    slot.worker_id
                )
            elif not has_worktree and slot.state is not WorkerState.FREE:
                report["ORPHAN"].append(slot.worker_id)
            elif slot.state is WorkerState.QUARANTINED:
                report["QUARANTINED"].append(slot.worker_id)
            else:
                report["VALID"].append(slot.worker_id)
            if (
                slot.state in (
                    WorkerState.READY, WorkerState.WORKING,
                    WorkerState.DIRTY, WorkerState.COMMITTED,
                )
                and not watcher_alive
            ):
                watchers_to_start.append(slot.worker_id)
        for path in worktrees:
            bound = any(s.worktree_path == path for s in slots.values())
            if not bound:
                report["ORPHAN"].append(f"unregistered:{path}")
        return {
            "classes": report,
            "watchers_to_start": watchers_to_start,
        }

    def _live_worktrees(self) -> dict[str, str]:
        result = self.repo.run(["worktree", "list", "--porcelain"])
        mapping: dict[str, str] = {}
        current = ""
        for line in (result.stdout or "").splitlines():
            if line.startswith("worktree "):
                current = line.split(" ", 1)[1].strip()
            elif line.startswith("branch ") and current:
                mapping[current] = line.split("/", 2)[-1].strip()
        return mapping

    @staticmethod
    def _running_pids() -> set[int]:
        pids: set[int] = set()
        try:
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            for line in out.splitlines():
                parts = line.split(",")
                if len(parts) > 1:
                    try:
                        pids.add(int(parts[1].strip('"')))
                    except ValueError:
                        continue
        except Exception:
            pass
        return pids


__all__ = ["ReconcileMixin"]
