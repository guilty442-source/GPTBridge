"""Worktree-aware repo snapshot capture for git tier audit records.

Keeps `subprocess` and I/O out of the protected `git_tiers` authority file.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _git(args: list[str], *, cwd: Path = _PROJECT_ROOT) -> str:
    """Run a git command and return stdout (stripped). Empty on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _capture_repo_snapshot(cwd: Path = _PROJECT_ROOT) -> dict[str, object]:
    """Capture worktree identity and patch digests for recovery evidence."""
    root = Path(cwd).resolve()
    head = _git(["rev-parse", "HEAD"], cwd=root)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root) or "HEAD"
    staged = _git(["diff", "--cached", "--name-only"], cwd=root)
    dirty = _git(["diff", "--name-only"], cwd=root)
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    staged_patch = _git(["diff", "--cached", "--binary"], cwd=root)
    unstaged_patch = _git(["diff", "--binary"], cwd=root)
    return {
        "repository": str(root),
        "head_revision": head,
        "branch": branch,
        "staged_files": staged.splitlines() if staged else [],
        "dirty_files": dirty.splitlines() if dirty else [],
        "untracked_files": untracked.splitlines() if untracked else [],
        "staged_patch_hash": hashlib.sha256(staged_patch.encode("utf-8")).hexdigest(),
        "unstaged_patch_hash": hashlib.sha256(unstaged_patch.encode("utf-8")).hexdigest(),
    }
