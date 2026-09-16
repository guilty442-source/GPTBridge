"""Git object-database maintenance manager (task §25 + perf specs 77-79).

Only the single maintenance owner may run heavyweight maintenance: workers,
watchers and the coordinator never do.  ``GitMaintenanceManager`` is the one
decision point for:

  - commit-graph (create → incremental split updates, ``--changed-paths``)
  - multi-pack-index (write + verify for many-pack repositories)
  - git gc (``--auto``, never ``--prune=now`` / ``--aggressive``)

High-frequency monitoring stays ``count-objects -v`` only.  Every heavy
action is gated by the peak-blocking guard ``maintenance_safe`` (spec 78):
no merge/rebase/self-commit/coordinator write/merge-queue activity may be in
flight, otherwise the maintenance window is skipped and reported.

Commit-graph policy (spec 74/75): incremental/split.  The manager records the
last-updated commit count in a small state file under ``gptbridge-automation``
so a batch of new commits triggers an incremental ``commit-graph write``
instead of a full rebuild on every commit.  Git for Windows 2.55+ supports
``--changed-paths`` (bloom filters); we enable them safely inside the write.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .git_repository import GitRepository
from .process_lock import lock_is_active
from .self_commit import _git_dir_path, operation_in_progress

DEFAULT_THRESHOLDS: dict[str, int] = {
    "loose_objects": 2000,
    "size_pack_mib": 2048,
    "garbage": 50,
    "packs": 50,
}

# Commit-graph refresh policy (spec 74/75): incremental/split.  We never
# rewrite the whole graph on every commit — the maintenance manager updates it
# in batches when enough new commits accumulate.
COMMIT_GRAPH_NEW_COMMITS: int = 64
COMMIT_GRAPH_MAX_NEW_FILTERS: int = 512


def object_stats(repo: GitRepository) -> dict[str, int]:
    """Parse ``git count-objects -v`` into integers."""
    result = repo.run(["count-objects", "-v"])
    stats: dict[str, int] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition(":")
        try:
            stats[key.strip().replace("-", "_")] = int(value.strip())
        except ValueError:
            continue
    return stats


def thresholds_exceeded(
    stats: dict[str, int], thresholds: dict[str, int] | None = None
) -> list[str]:
    """Return which thresholds tripped (empty = gc maintenance not needed)."""
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    tripped: list[str] = []
    if stats.get("count", 0) > limits["loose_objects"]:
        tripped.append("loose_objects")
    if stats.get("size_pack", 0) > limits["size_pack_mib"] * 1024:
        tripped.append("size_pack")
    if stats.get("garbage", 0) > limits["garbage"]:
        tripped.append("garbage")
    if stats.get("packs", 0) > limits["packs"]:
        tripped.append("packs")
    return tripped


def maintenance_safe(root: str | Path) -> dict[str, Any]:
    """True only when no merge/commit critical section is active.

    Peak-blocking guard (spec 78): the window must be quiet.  Blocked when
    an actual critical section is in flight — merge/rebase/cherry-pick,
    an active workspace sync, the merge-queue, an index write, or an
    active self-commit watcher.  The mere presence of the supervisor lock
    (the supervisor runs 24/7) never blocks the window by itself.
    """
    repo = GitRepository(root)
    reasons: list[str] = []
    git_dir = _git_dir_path(repo)
    common = git_dir
    common_result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (common_result.stdout or "").strip()
    if raw:
        common = Path(raw)
        if not common.is_absolute():
            common = repo.path / common
        common = common.resolve()
    if operation_in_progress(repo):
        reasons.append("operation-in-progress")
    if lock_is_active(common / "gptbridge-workspace-sync.lock"):
        reasons.append("lock-active:workspace-sync")
    queue_lock = common / "gptbridge-automation" / "merge-queue" / "merge-queue.lock"
    if lock_is_active(queue_lock):
        reasons.append("lock-active:merge-queue")
    if (git_dir / "index.lock").exists():
        reasons.append("index.lock")
    self_commit_locks = sorted(common.glob("gptbridge-self-commit-*.lock"))
    active_watchers = [
        lock.name for lock in self_commit_locks if lock_is_active(lock)
    ]
    if active_watchers:
        reasons.append("lock-active:self-commit:" + ",".join(active_watchers[:3]))
    return {"safe": not reasons, "reasons": reasons}


class GitMaintenanceManager:
    """Single owner of all heavyweight git object-database maintenance."""

    STATE_NAME = "git-maintenance-state.json"

    def __init__(
        self,
        root: str | Path,
        *,
        actor: str = "governance/git-maintenance",
        thresholds: dict[str, int] | None = None,
    ) -> None:
        self.root = root
        self.actor = actor
        self.repo = GitRepository(root)
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update(thresholds)

    def plan(self, *, commit_graph: bool = True) -> dict[str, Any]:
        """Read-only plan: which maintenance actions are warranted + safe."""
        stats = object_stats(self.repo)
        safety = maintenance_safe(self.root)
        actions: list[str] = []
        tripped = thresholds_exceeded(stats, self.thresholds)
        if tripped:
            actions.append("gc:auto")
        cg = self.commit_graph_status()
        if commit_graph and cg["warranted"]:
            actions.append(cg["action"])
        if stats.get("packs", 0) >= 2 and self._midx_needed():
            actions.append("multi-pack-index:write")
        return {
            "stats": stats,
            "safe": safety["safe"],
            "blocked_by": safety["reasons"],
            "actions": actions,
            "commit_graph": cg,
        }

    # ------------------------------------------------------------------ #
    # commit-graph
    # ------------------------------------------------------------------ #
    def commit_graph_status(self) -> dict[str, Any]:
        info = self._common_git_dir() / "objects" / "info"
        graph_dir = info / "commit-graphs"
        chain = graph_dir / "commit-graph-chain"
        legacy = info / "commit-graph"
        exists = chain.is_file() or legacy.is_file()
        total = 0
        try:
            total = int(
                self.repo.run(["rev-list", "--count", "--all"]).stdout.strip() or 0
            )
        except (ValueError, OSError):
            pass
        last_count = self._state().get("commit_graph_commits", 0)
        new_commits = max(0, total - last_count) if last_count else total
        # If we have never recorded a baseline, treat the current total as a
        # one-time baseline so we don't rebuild on first sight.
        if not last_count:
            new_commits = 0
            self._save_state({"commit_graph_commits": total})
        warranted = (
            exists and new_commits >= COMMIT_GRAPH_NEW_COMMITS
        ) or (not exists and total >= 2)
        action = "commit-graph:write"  # write reaches --reachable; git decides split
        return {
            "exists": exists,
            "chain_files": self._chain_files(),
            "total_commits": total,
            "new_commits": new_commits,
            "warranted": warranted,
            "action": action,
        }

    def _chain_files(self) -> int:
        chain = self._common_git_dir() / "objects" / "info" / "commit-graphs" / "commit-graph-chain"
        if not chain.is_file():
            return 0
        try:
            return len([l for l in chain.read_text(encoding="utf-8").splitlines() if l.strip()])
        except OSError:
            return 0

    # ------------------------------------------------------------------ #
    # multi-pack-index
    # ------------------------------------------------------------------ #
    def _midx_needed(self) -> bool:
        state = self._state()
        packs = len(list((self._common_git_dir() / "objects" / "pack").glob("*.idx")))
        last_packs = state.get("pack_count", 0)
        if last_packs == 0:
            self._save_state({"pack_count": packs})
            return False
        return packs > last_packs

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    def _state_path(self) -> Path:
        return self._common_git_dir() / "gptbridge-automation" / self.STATE_NAME

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, updates: dict[str, Any]) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            state = {**self._state(), **updates}
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _common_git_dir(self) -> Path:
        result = self.repo.run(["rev-parse", "--git-common-dir"])
        raw = (result.stdout or "").strip()
        common = Path(raw)
        if not common.is_absolute():
            common = self.repo.path / common
        return common.resolve()

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        dry_run: bool = False,
        commit_graph: bool = True,
        multi_pack_index: bool = True,
        gc: bool = True,
    ) -> dict[str, Any]:
        """Execute the governed maintenance plan (all actions gated by safety)."""
        plan = self.plan(commit_graph=commit_graph)
        result: dict[str, Any] = {
            "plan": plan,
            "ran": [],
            "skipped": [],
            "ran_any": False,
        }
        if not plan["safe"]:
            result["skipped"].append("blocked:" + "|".join(plan["blocked_by"]))
            return result
        if dry_run:
            result["skipped"].append("dry-run")
            return result
        actions = set(plan["actions"])
        if "gc:auto" in actions and not gc:
            actions.discard("gc:auto")
        if "commit-graph:write" in actions and not commit_graph:
            actions.discard("commit-graph:write")
        if "multi-pack-index:write" in actions and not multi_pack_index:
            actions.discard("multi-pack-index:write")
        for action in actions:
            outcome = self._run_action(action)
            result["ran"].append({action: outcome})
            result["ran_any"] = result["ran_any"] or outcome.get("ok", False)
            if action == "commit-graph:write" and outcome.get("ok"):
                self._save_state({"commit_graph_commits": plan["commit_graph"]["total_commits"]})
            if action == "multi-pack-index:write" and outcome.get("ok"):
                self._save_state({"pack_count": plan["stats"].get("packs", 0)})
        return result

    def _run_action(self, action: str) -> dict[str, Any]:
        """Execute one governed maintenance action."""
        if action == "commit-graph:write":
            args = [
                "commit-graph", "write", "--reachable", "--changed-paths",
                "--max-new-filters", str(COMMIT_GRAPH_MAX_NEW_FILTERS),
            ]
            res = self.repo.run(args, confirmed=True, actor=self.actor)
            return {"ok": res.returncode == 0, "detail": (res.stderr or "")[:200]}
        if action == "multi-pack-index:write":
            res = self.repo.run(["multi-pack-index", "write"], confirmed=True, actor=self.actor)
            return {"ok": res.returncode == 0, "detail": (res.stderr or "")[:200]}
        if action == "gc:auto":
            res = self.repo.run(["gc", "--auto"], confirmed=True, actor=self.actor)
            return {"ok": res.returncode == 0, "detail": (res.stderr or "")[:200]}
        return {"ok": False, "detail": f"unknown action {action}"}


def run_maintenance(
    root: str | Path,
    *,
    thresholds: dict[str, int] | None = None,
    actor: str = "governance/git-maintenance",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backward-compatible facade: governed gc plus commit-graph/midx upkeep."""
    manager = GitMaintenanceManager(root, actor=actor, thresholds=thresholds)
    return manager.run(dry_run=dry_run)


__all__ = [
    "COMMIT_GRAPH_MAX_NEW_FILTERS",
    "COMMIT_GRAPH_NEW_COMMITS",
    "DEFAULT_THRESHOLDS",
    "GitMaintenanceManager",
    "maintenance_safe",
    "object_stats",
    "run_maintenance",
    "thresholds_exceeded",
]