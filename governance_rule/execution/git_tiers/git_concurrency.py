"""Git read/write concurrency scopes (task §60/§61).

Tier-1 reads (status, rev-parse, diff, show) run in parallel up to
``max_parallel_git_reads`` — a bounded semaphore, never a global
exclusive lock, so 30 workers cannot spawn unbounded git.exe bursts
yet still share the pool.

Writes are scoped, not global:

    main_write_lock              — every write to main; single-flight
    worktree_write_lock:<id>     — serializes writes per worktree;
                                   different worktrees commit in
                                   parallel up to self-commit cap
    common_git_lock              — shared-object mutations (ref
                                   updates outside a worktree)
    audit_lock / queue_lock      — existing ledger/queue scopes

Self-commit parallelism is bounded by ``max_parallel_self_commit``.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

MAIN_WRITE_LOCK = "main-write"
COMMON_GIT_LOCK = "common-git"
WORKTREE_WRITE_PREFIX = "worktree-write:"


class GitConcurrency:
    """In-process read semaphore + scoped write locks."""

    def __init__(
        self,
        *,
        max_parallel_git_reads: int = 16,
        max_parallel_self_commit: int = 4,
        max_parallel_merge: int = 1,
    ) -> None:
        self._read_sem = threading.BoundedSemaphore(
            max_parallel_git_reads
        )
        self._commit_sem = threading.BoundedSemaphore(
            max_parallel_self_commit
        )
        self._merge_sem = threading.BoundedSemaphore(max_parallel_merge)
        self._main_lock = threading.Lock()
        self._common_lock = threading.Lock()
        self._worktree_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def read(self) -> Iterator[None]:
        """Bounded parallel Tier-1 read slot."""
        self._read_sem.acquire()
        try:
            yield
        finally:
            self._read_sem.release()

    @contextmanager
    def main_write(self) -> Iterator[None]:
        """Single-flight write scope for main (§61)."""
        with self._main_lock:
            yield

    @contextmanager
    def common_write(self) -> Iterator[None]:
        """Shared-object write scope (refs outside any worktree)."""
        with self._common_lock:
            yield

    @contextmanager
    def worktree_write(self, worker_id: str) -> Iterator[None]:
        """Serialize writes inside one worktree; parallel across
        worktrees up to the self-commit cap (§61)."""
        lock = self._lock_for(worker_id)
        self._commit_sem.acquire()
        try:
            with lock:
                yield
        finally:
            self._commit_sem.release()

    @contextmanager
    def merge_slot(self) -> Iterator[None]:
        """Single-flight main merge critical section (§54)."""
        self._merge_sem.acquire()
        try:
            yield
        finally:
            self._merge_sem.release()

    def _lock_for(self, worker_id: str) -> threading.Lock:
        with self._guard:
            lock = self._worktree_locks.get(worker_id)
            if lock is None:
                lock = threading.Lock()
                self._worktree_locks[worker_id] = lock
            return lock


__all__ = [
    "COMMON_GIT_LOCK",
    "GitConcurrency",
    "MAIN_WRITE_LOCK",
    "WORKTREE_WRITE_PREFIX",
]
