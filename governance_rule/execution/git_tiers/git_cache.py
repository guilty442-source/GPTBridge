"""Short-TTL cache for high-frequency Tier-1 Git queries (task §28).

Suitable for caching: HEAD, current branch, worktree list, branch
occupancy, last audit sequence — read-only lookups repeated many times per
second by watchers and health checks.

Never cached here: merge decisions, write authorization, long-lived dirty
state, security/governance decisions.

Every cache entry is keyed on (git common dir, worktree path, query name)
plus the HEAD/index metadata fingerprint captured at fill time, and lives
only for ``ttl`` seconds.  ``invalidate()`` is called by GitRepository.run
after every successful Tier-2/3 write so writes never serve stale reads.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .governance_manifest import timing as _manifest_timing

DEFAULT_TTL: float = _manifest_timing("git_cache_default_ttl_seconds", 2.0)

_store: dict[tuple[str, str, str], tuple[float, str, Any]] = {}
_lock = threading.Lock()


def _fingerprint(repo_path: Path) -> str:
    """HEAD + index metadata — cheap and changes on any commit/stage."""
    try:
        head_file = repo_path / ".git"
        if head_file.is_file():  # worktree: .git is a file pointer
            head_file = repo_path
        git_dir = head_file if head_file.is_dir() else repo_path / ".git"
        parts: list[str] = []
        head = git_dir / "HEAD"
        index = git_dir / "index"
        for candidate in (head, index):
            try:
                stat = candidate.stat()
                parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                parts.append("-")
        return "|".join(parts)
    except OSError:
        return ""


def cached(
    repo_path: str | Path,
    name: str,
    producer: Callable[[], Any],
    *,
    common_dir: str | Path = "",
    ttl: float = DEFAULT_TTL,
) -> Any:
    """Return a cached Tier-1 query result, filling via ``producer``."""
    key = (str(common_dir), str(Path(repo_path).resolve()), str(name))
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is not None:
            expires, fingerprint, value = entry
            if now < expires and fingerprint == _fingerprint(Path(repo_path)):
                return value
    value = producer()
    fingerprint = _fingerprint(Path(repo_path))
    with _lock:
        _store[key] = (now + max(0.1, float(ttl)), fingerprint, value)
    return value


def invalidate(repo_path: str | Path | None = None) -> None:
    """Drop cached entries — everything, or just one worktree's."""
    with _lock:
        if repo_path is None:
            _store.clear()
            return
        target = str(Path(repo_path).resolve())
        for key in [k for k in _store if k[1] == target]:
            _store.pop(key, None)


def stats() -> dict[str, int]:
    with _lock:
        return {"entries": len(_store)}


__all__ = ["DEFAULT_TTL", "cached", "invalidate", "stats"]
