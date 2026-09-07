"""Worktree-aware repo snapshot capture for git tier audit records.

Keeps `subprocess` and I/O out of the protected `git_tiers` authority file.
"""
from __future__ import annotations

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


def _capture_repo_snapshot() -> dict[str, object]:
    """Capture pre-operation repo state for recovery metadata.

    Records:
      - head_revision: current HEAD SHA (recovery target on failure)
      - branch: current branch name (or "HEAD" if detached)
      - dirty_files: list of unstaged-modified file paths
      - staged_files: list of staged file paths
    """
    head = _git(["rev-parse", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD"
    staged = _git(["diff", "--cached", "--name-only"])
    dirty = _git(["diff", "--name-only"])
    return {
        "head_revision": head,
        "branch": branch,
        "staged_files": staged.splitlines() if staged else [],
        "dirty_files": dirty.splitlines() if dirty else [],
    }
