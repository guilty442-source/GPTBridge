"""Lock hierarchy for Git automation (task §31).

Fixed acquisition order — a context may only acquire a lock whose rank is
strictly greater than every lock it already holds:

    1. supervisor registry lock
    2. workspace sync lock
    3. merge queue lock
    4. worktree lock
    5. audit append lock

Reverse-order acquisition raises ``LockOrderError`` (fail-closed) instead
of risking a deadlock between supervisor, coordinator, self-commit and
audit writers.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Final, Iterator

LOCK_ORDER: Final[tuple[str, ...]] = (
    "supervisor-registry",
    "workspace-sync",
    "merge-queue",
    "worktree",
    "audit-append",
)

LOCK_RANK: Final[dict[str, int]] = {
    name: index + 1 for index, name in enumerate(LOCK_ORDER)
}

_held = threading.local()


class LockOrderError(RuntimeError):
    """Raised when a lock would be acquired out of hierarchy order."""


def _held_ranks() -> list[tuple[str, int]]:
    return getattr(_held, "stack", [])


def assert_order(name: str) -> None:
    """Assert acquiring ``name`` now would respect LOCK_ORDER."""
    rank = LOCK_RANK.get(str(name))
    if rank is None:
        raise LockOrderError(f"unregistered lock: {name}")
    for held_name, held_rank in _held_ranks():
        if held_rank >= rank:
            raise LockOrderError(
                f"lock-order violation: {name}({rank}) while holding "
                f"{held_name}({held_rank})"
            )


@contextmanager
def ordered(name: str, lock) -> Iterator:
    """Acquire ``lock`` (a ProcessFileLock-like context manager) in order."""
    assert_order(name)
    stack = _held_ranks()
    with lock:
        stack = stack + [(name, LOCK_RANK[str(name)])]
        _held.stack = stack
        try:
            yield
        finally:
            _held.stack = stack[:-1]


__all__ = [
    "LOCK_ORDER",
    "LOCK_RANK",
    "LockOrderError",
    "assert_order",
    "ordered",
]
