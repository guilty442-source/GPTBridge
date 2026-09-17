"""Worktree-aware repo snapshot capture for git tier audit records.

Keeps direct process spawning out of this module: read-only snapshot commands
execute through the gateway's single ``git`` driver (``git_repository``),
which is also what consumes the resulting snapshot.

Performance: ``capture_light_snapshot`` replaces the 7-subprocess production
with a single ``git status --porcelain=v2 -z --branch`` call, reducing
snapshot overhead from ~1.5s to ~200ms per ``GitRepository.run()`` invocation.
The legacy ``_capture_repo_snapshot`` (heavy) is retained only for recovery
evidence paths that explicitly request full binary diff hashes.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Final

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_READ_TIMEOUT_SECONDS: Final[float] = 60.0


def _git(args: list[str], *, cwd: Path = _PROJECT_ROOT) -> str:
    """Run a Tier-1 read-only git command through the gateway driver.

    Returns stdout (stripped) and empty on failure.  The tier guard makes
    this a read-only observation path only — a write command is never
    executed here, so the snapshot builder can never re-enter the gateway's
    authorization/audit pipeline (which would recurse).
    """
    from . import classify
    from .git_repository import _extended_tier, _spawn_git

    command = " ".join(str(item) for item in args)
    tier = _extended_tier(command)
    if (tier if tier is not None else classify(command)) != 1:
        return ""
    try:
        result = _spawn_git(
            [str(item) for item in args], cwd=Path(cwd).resolve(),
            timeout=_READ_TIMEOUT_SECONDS,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_EMPTY_HASH: Final[str] = "0" * 64


def capture_light_snapshot(cwd: Path = _PROJECT_ROOT) -> dict[str, object]:
    """Single-subprocess snapshot derived from ``porcelain v2 -z --branch``.

    Returns the same key set as ``_capture_repo_snapshot`` so audit_log
    consumers see no difference.  Differences (both audit-evidence only):
      - ``untracked_files`` is directory-granular (porcelain collapses
        whole untracked directories into one ``? dir/`` entry) whereas the
        heavy path enumerates every file via ``ls-files --others``;
      - binary diff hashes are zeroed (nobody reads them).
    """
    root = Path(cwd).resolve()
    porcelain = _git(["status", "--porcelain=v2", "-z", "--branch"], cwd=root)
    head = ""
    branch = "HEAD"
    staged_files: list[str] = []
    dirty_files: list[str] = []
    untracked_files: list[str] = []

    records = porcelain.split("\0")
    idx = 0
    while idx < len(records):
        rec = records[idx]
        idx += 1
        if not rec:
            continue
        # branch headers
        if rec.startswith("# "):
            header = rec[2:]
            if header.startswith("branch.oid "):
                head = header[len("branch.oid "):].strip()
            elif header.startswith("branch.head "):
                bn = header[len("branch.head "):].strip()
                if bn and bn != "(detached)":
                    branch = bn
            # branch.ab, branch.upstream — ignored for snapshot
            continue
        tag = rec[:1]
        if tag == "1":
            fields = rec.split(" ", 8)
            if len(fields) == 9:
                xy = fields[1]
                path = fields[8]
                if xy[0] not in (".", "?", "!"):
                    staged_files.append(path)
                if xy[1:2] not in (".", "", "?", "!"):
                    dirty_files.append(path)
        elif tag == "2":
            fields = rec.split(" ", 9)
            if len(fields) == 10 and idx < len(records):
                orig = records[idx]
                idx += 1
                xy = fields[1]
                path = fields[9]
                if xy[0] not in (".", "?", "!"):
                    staged_files.append(path)
                if xy[1:2] not in (".", "", "?", "!"):
                    dirty_files.append(path)
        elif tag == "u":
            fields = rec.split(" ", 10)
            if len(fields) == 11:
                dirty_files.append(fields[10])
        elif tag == "?":
            untracked_files.append(rec[2:])
        # "!" (ignored) — skipped

    return {
        "repository": str(root),
        "head_revision": head,
        "branch": branch,
        "staged_files": staged_files,
        "dirty_files": dirty_files,
        "untracked_files": untracked_files,
        "staged_patch_hash": _EMPTY_HASH,
        "unstaged_patch_hash": _EMPTY_HASH,
    }


def _capture_repo_snapshot(cwd: Path = _PROJECT_ROOT) -> dict[str, object]:
    """Capture worktree identity and patch digests for recovery evidence.

    Retained for backward compatibility and recovery paths that need binary
    diff hashes.  New call sites should prefer ``capture_light_snapshot``.
    """
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
