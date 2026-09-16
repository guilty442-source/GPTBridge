"""Branch reconciliation / orphan detection (task §27).

Every local and remote branch is classified into exactly one state:

    PROTECTED        policy-protected branch (main + persistent pool)
    ACTIVE_WORKTREE  checked out by a registered worktree
    QUEUED           has a live merge-queue entry
    MERGED           fully contained in main
    UNMERGED         local branch with commits not in main
    ORPHAN           no worktree, no queue entry, unmerged (evidence only)
    REMOTE_ONLY      exists only under refs/remotes

Ephemeral branches that are fully merged, have no worktree, no queue entry
and exceed the retention age become ``retirement_candidate`` — a report,
not an action; actual deletion stays Tier-3 governed.  Persistent and
protected branches are never retirement candidates.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .branch_policy import can_auto_retire, is_protected, normalize_branch
from .git_repository import GitRepository
from .merge_queue import MergeQueue
from .worktree_manager import WorktreeManager

BRANCH_STATES = frozenset(
    {
        "ACTIVE_WORKTREE",
        "QUEUED",
        "MERGED",
        "UNMERGED",
        "ORPHAN",
        "PROTECTED",
        "REMOTE_ONLY",
    }
)
RETENTION_SECONDS: float = 7 * 24 * 3600.0


def _local_branches(repo: GitRepository) -> dict[str, tuple[str, float]]:
    """Local branches: name -> (sha, committerdate-epoch)."""
    result = repo.run(
        ["for-each-ref", "refs/heads", "--format=%(refname) %(objectname) %(committerdate:unix)"]
    )
    out: dict[str, tuple[str, float]] = {}
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            name = normalize_branch(parts[0])
            out[name] = (parts[1], float(parts[2]) if parts[2].isdigit() else 0.0)
    return out


def _remote_only(repo: GitRepository) -> list[str]:
    result = repo.run(
        ["for-each-ref", "refs/remotes", "--format=%(refname)"]
    )
    names: list[str] = []
    for line in (result.stdout or "").splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        names.append(normalize_branch(ref))
    return names


def reconcile_branches(root: str | Path) -> dict[str, Any]:
    """Classify every branch; return states + retirement candidates."""
    repo = GitRepository(root)
    manager = WorktreeManager(repo)
    worktree_branches = {
        normalize_branch(item.get("branch", ""))
        for item in manager.list_worktrees()
        if item.get("branch")
    }
    queue = MergeQueue(root)
    queued = {
        entry.get("source_branch")
        for entry in queue.entries({"pending", "running", "conflicted", "blocked"})
    }

    ages = _local_branches(repo)
    locals_ = {name: sha for name, (sha, _ts) in ages.items()}
    remote_names = set(_remote_only(repo))
    merged_into_main = {
        name
        for name in locals_
        if name != "main"
        and repo.run(["merge-base", "--is-ancestor", name, "main"]).returncode == 0
    }

    now = time.time()
    states: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []
    for name in sorted(locals_):
        if is_protected(name):
            states[name] = "PROTECTED"
            continue
        if name in worktree_branches:
            states[name] = "ACTIVE_WORKTREE"
            continue
        if name in queued:
            states[name] = "QUEUED"
            continue
        merged = name in merged_into_main
        states[name] = "MERGED" if merged else "ORPHAN"
        if (
            merged
            and can_auto_retire(name)
            and now - ages.get(name, ("", now))[1] > RETENTION_SECONDS
        ):
            candidates.append(
                {
                    "branch": name,
                    "sha": locals_[name],
                    "age_seconds": round(now - ages.get(name, ("", now))[1], 1),
                    "reason": "ephemeral+merged+no-worktree+retention-exceeded",
                }
            )
    for name in sorted(remote_names - set(locals_)):
        states.setdefault(name, "REMOTE_ONLY")

    return {
        "states": states,
        "retirement_candidates": candidates,
        "queued": sorted(b for b in queued if b),
    }


__all__ = ["BRANCH_STATES", "RETENTION_SECONDS", "reconcile_branches"]
