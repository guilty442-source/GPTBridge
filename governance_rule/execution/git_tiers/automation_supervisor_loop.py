"""Supervision loop and per-worktree watcher management.

Spawns one self-commit watcher subprocess per registered worktree with
bounded restart backoff (A170/A178), runs the conflict-safe synchronizer on
a cadence, and exits cleanly when the lock file is removed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .automation_supervisor_state import (
    SUPERVISOR_ACTOR,
    SUPERVISOR_LOCK,
    _branch_of,
    _lock_is_active_here,
    _setup_logging,
    _state_dir,
    _terminate_tree,
    _write_registry,
)
from .git_repository import GitRepository
from .process_lock import LockBusyError, ProcessFileLock
from .worktree_manager import WorktreeManager
from .workspace_sync import synchronize


class _Watcher:
    """One supervised self-commit watcher subprocess."""

    def __init__(
        self,
        worktree: str,
        branch: str,
        process: subprocess.Popen[str],
        log_path: Path,
    ) -> None:
        self.worktree = worktree
        self.branch = branch
        self.process = process
        self.log_path = log_path
        self.started_at = time.time()
        self.restarts = 0
        self.dead_since: float | None = None

    def alive(self) -> bool:
        return self.process.poll() is None

    def restart_delay(self) -> float:
        return min(300.0, 5.0 * (2 ** self.restarts))

    def to_dict(self) -> dict[str, object]:
        return {
            "worktree": self.worktree,
            "branch": self.branch,
            "pid": self.process.pid,
            "log": str(self.log_path),
            "started_at": self.started_at,
            "restarts": self.restarts,
        }


def _spawn_watcher(
    root: str | Path,
    worktree: str,
    branch: str,
    directory: Path,
    interval: float,
    debounce: float,
) -> _Watcher:
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in branch)
    log_path = logs / f"watcher-{safe}.log"
    log_file = log_path.open("a", encoding="utf-8")
    module = "governance_rule.execution.git_tiers.self_commit"
    cmd = [
        sys.executable,
        "-m",
        module,
        "--worktree",
        worktree,
        "--watch",
        "--interval",
        str(int(interval)),
        "--debounce",
        str(int(debounce)),
        "--actor",
        SUPERVISOR_ACTOR,
    ]
    process = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=log_file,
        stderr=log_file,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _Watcher(worktree, branch, process, log_path)


def supervise(
    root: str | Path,
    *,
    sync_interval: float = 60.0,
    health_interval: float = 20.0,
    watch_interval: float = 30.0,
    debounce: float = 60.0,
    commit_dirty: bool = False,
    push: bool = False,
) -> None:
    """Supervise worktree watchers and run periodic synchronization.

    Blocks until the process receives a stop (``--stop`` removes the lock and
    the loop exits once the lock is released).  When another supervisor
    already owns the lock this returns immediately instead of crashing.
    """
    directory = _state_dir(root)
    logger = _setup_logging(root, directory, "supervisor")
    lock_path = directory / SUPERVISOR_LOCK
    lock = ProcessFileLock(lock_path)

    try:
        with lock:
            supervise_loop(root, logger, lock_path, directory,
                            sync_interval=sync_interval,
                            health_interval=health_interval,
                            watch_interval=watch_interval,
                            debounce=debounce,
                            commit_dirty=commit_dirty, push=push)
    except LockBusyError:
        logger.info("another supervisor owns the lock; exiting")
        return


def _refresh_watchers(
    root: str | Path,
    manager: WorktreeManager,
    watchers: dict[str, _Watcher],
    directory: Path,
    logger: logging.Logger,
    watch_interval: float,
    debounce: float,
) -> None:
    """Reconcile supervised watchers with the live worktree list."""
    worktrees = manager.list_worktrees()
    known = {Path(item["path"]).resolve() for item in worktrees}

    for watcher_path in list(watchers):
        if Path(watcher_path).resolve() not in known:
            watchers.pop(watcher_path)

    for item in worktrees:
        resolved = str(Path(item["path"]).resolve())
        existing = watchers.get(resolved)
        if existing is not None:
            if existing.alive():
                continue
            if existing.dead_since is None:
                existing.dead_since = time.time()
            if (time.time() - existing.dead_since) < existing.restart_delay():
                continue
            logger.warning("restarting watcher pid=%d %s",
                           existing.process.pid, resolved)
            _terminate_tree(existing.process.pid)
            watcher = _spawn_watcher(
                root, resolved, _branch_of(root, resolved),
                directory, watch_interval, debounce,
            )
            watcher.restarts = existing.restarts + 1
            watchers[resolved] = watcher
            continue
        branch = _branch_of(root, resolved)
        watcher = _spawn_watcher(
            root, resolved, branch, directory,
            watch_interval, debounce,
        )
        watchers[resolved] = watcher
        logger.info("spawned watcher pid=%d %s (%s)",
                    watcher.process.pid, resolved, branch)


def _run_sync_cycle(
    root: str | Path,
    registry: dict[str, object],
    logger: logging.Logger,
    *,
    commit_dirty: bool,
    push: bool,
) -> float:
    """Run one synchronization cycle and record its result."""
    last_sync_at = time.time()
    registry["sync_cycles"] = int(registry.get("sync_cycles", 0)) + 1
    try:
        result = synchronize(root, commit_dirty=commit_dirty, push=push)
    except Exception as exc:  # keep the loop alive
        result = f"error:{type(exc).__name__}"
    registry["last_sync"] = time.time()
    registry["last_sync_result"] = result
    logger.info("sync cycle %s -> %s", registry["sync_cycles"], result)
    return last_sync_at


def _initial_registry(
    *,
    sync_interval: float,
    health_interval: float,
    commit_dirty: bool,
    push: bool,
) -> dict[str, object]:
    return {
        "pid": os.getpid(),
        "started_at": time.time(),
        "sync_interval": sync_interval,
        "health_interval": health_interval,
        "commit_dirty": commit_dirty,
        "push": push,
        "last_sync": "",
        "last_sync_result": "",
        "sync_cycles": 0,
        "children": [],
    }


def supervise_loop(
    root: str | Path,
    logger: logging.Logger,
    lock_path: Path,
    directory: Path,
    *,
    sync_interval: float,
    health_interval: float,
    watch_interval: float,
    debounce: float,
    commit_dirty: bool,
    push: bool,
) -> None:
    """Run the supervision loop.  The caller owns ``lock_path``."""
    manager = WorktreeManager(GitRepository(root))
    registry = _initial_registry(
        sync_interval=sync_interval,
        health_interval=health_interval,
        commit_dirty=commit_dirty,
        push=push,
    )
    watchers: dict[str, _Watcher] = {}
    last_sync_at = time.time()
    logger.info("supervisor started pid=%d root=%s", os.getpid(), root)

    while True:
        try:
            _refresh_watchers(
                root, manager, watchers, directory, logger,
                watch_interval, debounce,
            )

            if time.time() - last_sync_at >= sync_interval:
                last_sync_at = _run_sync_cycle(
                    root, registry, logger,
                    commit_dirty=commit_dirty, push=push,
                )

            registry["children"] = [w.to_dict() for w in watchers.values()]
            _write_registry(directory, registry)

            if not lock_path.exists() or not _lock_is_active_here(lock_path):
                logger.info("stop requested; shutting down")
                break
        except Exception as exc:  # never let the supervisor die silently
            logger.error("supervisor loop error: %s", type(exc).__name__)
        time.sleep(health_interval)
    for watcher in watchers.values():
        _terminate_tree(watcher.process.pid)
    logger.info("supervisor stopped")
