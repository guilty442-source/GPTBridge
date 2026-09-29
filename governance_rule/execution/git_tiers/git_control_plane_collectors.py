"""Control-plane truth collectors (A185 split).

Extracted from ``git_control_plane`` (source-size contract): the
``_Collectors`` mixin owns every ``_collect_*`` source-of-truth read
(§166) plus the hook/policy digests and disk probe.  State lives on the
``GitControlPlane`` instance; this module only contributes methods.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from . import automation_supervisor_state
from . import git_perf
from .audit_chain import chain_health
from .git_repository import GitRepository
from .repo_sync import sync_state
from .merge_queue import MergeQueue
from .worker_registry import PoolRegistry
from .git_control_plane_types import HealthStatus, MAIN_REF


class _Collectors:
    """Dimensional fact collectors — one method per source of truth."""

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

    # -- collect dispatch -----------------------------------------------------

    def collect(self, name: str) -> dict[str, Any]:
        """Run one collector; failures degrade to UNKNOWN, never raise."""
        try:
            return dict(self._sources[name]())
        except Exception as exc:
            return {"error": str(exc), "unknown": True}

    def collect_all(self) -> dict[str, dict[str, Any]]:
        return {name: self.collect(name) for name in self._COLLECTORS}

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


__all__ = ["_Collectors"]
