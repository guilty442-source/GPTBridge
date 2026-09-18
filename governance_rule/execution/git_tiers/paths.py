"""Fail-closed path containment for governed Git artifacts (A201).

Every worker worktree and every backup artifact must resolve inside the
canonical project root.  Sibling directories at the drive root are never
valid targets, and no code path may create them.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_CODE_ROOT = Path(__file__).resolve().parents[3]

PERSISTENT_WORKTREES_DIRNAME = ".worktrees"
EPHEMERAL_WORKTREES_RELATIVE = Path(".kilo") / "worktrees"
BACKUP_ROOT_RELATIVE = Path(".backups") / "git"


class PathContainmentError(RuntimeError):
    """A governed artifact path escaped the canonical project root."""

    failure_code = "PATH_OUTSIDE_ROOT"


def contained(root: str | Path, target: str | Path, *, purpose: str = "path") -> Path:
    """Resolve ``target`` and require it to be ``root`` or a strict descendant.

    Relative targets are resolved against ``root``.  Symlinks, junctions and
    short names are normalized through ``Path.resolve`` before the boundary
    check.  Anything outside fails closed with ``PathContainmentError``.
    """
    resolved_root = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PathContainmentError(f"{purpose}-outside-root:{resolved}")
    return resolved


__all__ = [
    "BACKUP_ROOT_RELATIVE",
    "EPHEMERAL_WORKTREES_RELATIVE",
    "PERSISTENT_WORKTREES_DIRNAME",
    "PROJECT_CODE_ROOT",
    "PathContainmentError",
    "contained",
]
