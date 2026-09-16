"""Worker / task / branch identity (task §41/§42/§68/§69).

The supervisor issues every identity — an AI never picks its own
``worker_id`` and never turns user text into a branch name.

    worker_id        w001, w002, ... (pool-issued, sequential)
    instance_id      uuid per allocation — ownership is
                     ``w007:<instance-id>`` so a reused slot cannot
                     inherit stale PID/branch ownership (§68)
    task_id          caller-supplied stable id; retries share the
                     task_id with different attempt_id (§69)
    branch           ai/<domain>/<task-id>/<worker-id> — sanitized,
                     unique, length-capped, git-ref legal (§41)
    worktree path    <workers_root>/wNNN — task names never appear
                     in paths (§42: length/special-char/MAX_PATH safe)
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

BRANCH_MAX_LENGTH = 120
_WORKER_ID = re.compile(r"^w(\d{3,})$")
_SEGMENT_ALLOWED = re.compile(r"[^a-z0-9-]+")


def _sanitize_segment(value: str, *, max_len: int = 48) -> str:
    """Reduce arbitrary text to a safe branch segment.

    Lowercase ascii ``[a-z0-9-]``; strips accents, collapses repeats,
    trims edge dashes, caps length.  Never raises — degenerate input
    yields ``x`` rather than an attacker/typo-controlled name.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SEGMENT_ALLOWED.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text[:max_len].strip("-") or "x")


def parse_worker_number(worker_id: str) -> int | None:
    match = _WORKER_ID.match(worker_id)
    return int(match.group(1)) if match else None


def next_worker_id(existing: list[str]) -> str:
    """Issue the lowest free ``wNNN`` id — pool-owned, not AI-chosen."""
    used = {parse_worker_number(w) for w in existing}
    used.discard(None)
    number = 1
    while number in used:
        number += 1
    return f"w{number:03d}"


def build_branch_name(
    domain: str, task_id: str, worker_id: str
) -> str:
    """``ai/<domain>/<task>/<worker>`` — canonical governed name (§41)."""
    branch = "/".join(
        (
            "ai",
            _sanitize_segment(domain, max_len=24),
            _sanitize_segment(task_id, max_len=48),
            _sanitize_segment(worker_id, max_len=12),
        )
    )
    return branch[:BRANCH_MAX_LENGTH]


def worktree_path_for(workers_root: str | Path, worker_id: str) -> str:
    """``<workers_root>/wNNN`` — identity-stable, task-name-free (§42)."""
    return str(Path(workers_root) / worker_id)


def valid_git_branch(name: str) -> bool:
    """Minimal ref sanity: allowed chars, no ``..``, no trailing dot/slash."""
    if not name or len(name) > BRANCH_MAX_LENGTH:
        return False
    if name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if ".." in name or "//" in name or "@{" in name:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name))


def attempt_id_for(task_id: str, attempt: int) -> str:
    """``task-1842`` + ``attempt-2`` — retry keeps task, new attempt (§69)."""
    return f"{_sanitize_segment(task_id)}-attempt-{int(attempt)}"


__all__ = [
    "BRANCH_MAX_LENGTH",
    "attempt_id_for",
    "build_branch_name",
    "next_worker_id",
    "parse_worker_number",
    "valid_git_branch",
    "worktree_path_for",
]
