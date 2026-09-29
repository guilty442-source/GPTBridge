"""Self-commit cooperating-writers lease + git-dir primitives (A185 split).

Extracted from ``self_commit.py`` (source-size contract).  Owns:

  * git-dir helpers shared by the sweep and ``git_maintenance``
    (``_git_dir_path``, ``_is_main_worktree``, ``operation_in_progress``);
  * the §10.69-E② single-writer commit lease — a human/agent declares an
    in-progress commit sequence by writing a lease file inside .git; the
    self-commit sweep defers while any fresh lease is held, and declares
    its own lease for the add+commit window so lease-aware writers yield
    both ways.  Git itself never sees the file — this is a
    cooperating-writers contract, not a lock (HEAD can still move;
    writers must be prepared to retry ``cannot lock ref HEAD``).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .git_repository import GitRepository

SELF_COMMIT_ACTOR: str = "governance/self-commit"
IN_PROGRESS_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


def _git_dir_path(repo: GitRepository) -> Path:
    result = repo.run(["rev-parse", "--git-dir"])
    raw = (result.stdout or "").strip()
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = repo.path / resolved
    return resolved.resolve()


def _is_main_worktree(repo: GitRepository) -> bool:
    """Return True for the primary checkout, which Git does not allow locking."""
    common = repo.run(["rev-parse", "--git-common-dir"])
    raw = (common.stdout or "").strip()
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = repo.path / common_dir
    return _git_dir_path(repo) == common_dir.resolve()


def operation_in_progress(repo: GitRepository) -> bool:
    """True when a merge/rebase/cherry-pick/revert is underway (or sequencer)."""
    git_dir = _git_dir_path(repo)
    if any((git_dir / marker).exists() for marker in IN_PROGRESS_MARKERS):
        return True
    return bool((git_dir / "sequencer").exists())


COMMIT_LEASE_FILENAME = "self-commit-lease.json"
SELF_COMMIT_LEASE_TTL_SECONDS = 120.0


def _commit_lease_path(repo: GitRepository) -> Path:
    return _git_dir_path(repo) / COMMIT_LEASE_FILENAME


def commit_lease_active(repo: GitRepository) -> dict | None:
    """Fresh lease data when another writer is mid-commit, else None."""
    try:
        data = json.loads(
            _commit_lease_path(repo).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None
    try:
        if float(data.get("until") or 0) > time.time():
            return data
    except (TypeError, ValueError):
        pass
    return None


def claim_commit_lease(
    worktree: str | Path, *, owner: str, ttl_seconds: float = 300.0
) -> Path | None:
    """Declare an in-progress commit; renew before ttl, release when done.

    Workers/agents call this BEFORE ``git add`` so the self-commit sweep
    never stages their in-flight work under a generic message. Returns
    None when a fresh lease from another owner already exists — callers
    must defer instead of overwriting (the lease is a cooperating-writers
    contract, not a lock)."""
    repo = GitRepository(worktree)
    path = _commit_lease_path(repo)
    existing = commit_lease_active(repo)
    if existing is not None and existing.get("owner") != str(owner):
        return None
    path.write_text(
        json.dumps(
            {
                "owner": str(owner),
                "claimed_at": time.time(),
                "until": time.time() + max(1.0, float(ttl_seconds)),
            }
        ),
        encoding="utf-8",
    )
    return path


def release_commit_lease(worktree: str | Path, *, owner: str) -> None:
    """Release only the caller's own lease — never delete another
    writer's claim (a racing claim that landed after ours stays valid)."""
    repo = GitRepository(worktree)
    try:
        data = json.loads(
            _commit_lease_path(repo).read_text(encoding="utf-8")
        )
        if data.get("owner") != str(owner):
            return
        _commit_lease_path(repo).unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


__all__ = [
    "COMMIT_LEASE_FILENAME", "IN_PROGRESS_MARKERS", "SELF_COMMIT_ACTOR",
    "SELF_COMMIT_LEASE_TTL_SECONDS", "claim_commit_lease",
    "commit_lease_active", "operation_in_progress", "release_commit_lease",
    "_commit_lease_path", "_git_dir_path", "_is_main_worktree",
]
