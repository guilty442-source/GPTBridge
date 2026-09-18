"""Supervision loop and per-worktree watcher management.

Spawns one self-commit watcher subprocess per registered worktree with
bounded restart backoff (A441/A178), runs the conflict-safe synchronizer on
a cadence, and exits cleanly when the lock file is removed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .audit_chain import chained_audit_log
from .branch_policy import is_main, normalize_branch
from .automation_supervisor_state import (
    SUPERVISOR_ACTOR,
    SUPERVISOR_LOCK,
    _branch_of,
    _lock_is_active_here,
    _read_registry,
    _setup_logging,
    _state_dir,
    _terminate_tree,
    _write_registry,
)
from .git_repository import GitRepository
from .governance_manifest import (
    GovernanceWriteBlocked,
    assert_write_allowed,
    timing as _manifest_timing,
)
from .process_lock import LockBusyError, ProcessFileLock, pid_alive
from .worktree_manager import WorktreeManager
from .workspace_sync import synchronize

# Governed cadence defaults (manifest version source; A318-A320).
DEFAULT_SYNC_INTERVAL_SECONDS: float = _manifest_timing(
    "supervisor_sync_interval_seconds", 60.0
)
DEFAULT_HEALTH_INTERVAL_SECONDS: float = _manifest_timing(
    "supervisor_health_interval_seconds", 20.0
)
DEFAULT_WATCH_INTERVAL_SECONDS: float = _manifest_timing(
    "supervisor_watch_interval_seconds", 30.0
)
DEFAULT_DEBOUNCE_SECONDS: float = _manifest_timing(
    "supervisor_debounce_seconds", 60.0
)


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
        # TODO(inventory): wire to manifest timings.supervisor_restart_*.
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


def _jittered_debounce(base: float, salt: str) -> float:
    """Commit-storm control (spec 92): deterministic jitter around the base.

    Keeps watchers from all firing in the same second after their debounce
    expires simultaneously.  The jitter is a stable hash of the branch name,
    so it never changes governance semantics — it only staggers I/O peaks.
    """
    import hashlib

    digest = hashlib.sha256(salt.encode("utf-8")).hexdigest()
    # TODO(inventory): wire jitter spread (26s) to manifest timings.
    spread = int(digest[:8], 16) % 26  # 0..25 seconds
    return max(1.0, base + float(spread))


def supervise(
    root: str | Path,
    *,
    sync_interval: float = DEFAULT_SYNC_INTERVAL_SECONDS,
    health_interval: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
    watch_interval: float = DEFAULT_WATCH_INTERVAL_SECONDS,
    debounce: float = DEFAULT_DEBOUNCE_SECONDS,
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
        assert_write_allowed("automation-supervisor.supervise")
    except GovernanceWriteBlocked as exc:
        logger.error("governance write guard, supervisor not started: %s", exc)
        return

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
    """Reconcile supervised watchers with the live worktree list.

    The main checkout is never supervised by a self-commit watcher
    (acceptance §35 / A163: main is integration-only — only the
    coordinator may commit there via governed merges).
    """
    root_resolved = Path(root).resolve()
    worktrees = [
        item
        for item in manager.list_worktrees()
        if Path(item["path"]).resolve() != root_resolved
        and not is_main(item.get("branch", ""))
    ]
    known = {Path(item["path"]).resolve() for item in worktrees}

    for watcher_path in list(watchers):
        if Path(watcher_path).resolve() not in known:
            watchers.pop(watcher_path)

    for item in worktrees:
        resolved = str(Path(item["path"]).resolve())
        branch = _branch_of(root, resolved)
        effective_debounce = _jittered_debounce(debounce, branch)
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
                root, resolved, branch,
                directory, watch_interval, effective_debounce,
            )
            watcher.restarts = existing.restarts + 1
            watchers[resolved] = watcher
            continue
        watcher = _spawn_watcher(
            root, resolved, branch, directory,
            watch_interval, effective_debounce,
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


def _startup_reconciliation(
    root: str | Path,
    manager: WorktreeManager,
    logger: logging.Logger,
) -> None:
    """Crash recovery: never trust the registry left by a dead supervisor.

    Read registry -> verify PID liveness -> verify worktree/branch mapping
    -> verify lock ownership -> audit stale entries -> rebuild watcher
    mapping (done by the normal refresh afterwards).  Missing PIDs are
    audited as STALE_PROCESS_DETECTED, not silently dropped.
    """
    chained_audit_log(
        1, "supervisor startup reconciliation", SUPERVISOR_ACTOR, True,
        "RECOVERY_STARTED", operation="supervisor-recovery",
        phase="result", result="started",
    )
    previous = _read_registry(root)
    stale: list[int] = []
    if previous:
        known = {
            str(Path(item["path"]).resolve())
            for item in manager.list_worktrees()
        }
        for child in previous.get("children", []):
            pid = int(child.get("pid", 0) or 0)
            worktree = str(child.get("worktree", ""))
            alive = bool(pid) and pid_alive(pid)
            registered = (
                str(Path(worktree).resolve()) in known if worktree else False
            )
            if pid and not alive:
                stale.append(pid)
                chained_audit_log(
                    1, "supervisor stale watcher", SUPERVISOR_ACTOR, True,
                    f"STALE_PROCESS_DETECTED pid={pid} worktree={worktree}",
                    operation="supervisor-recovery",
                    phase="result", result="stale-process",
                )
            elif not registered:
                chained_audit_log(
                    1, "supervisor orphan watcher", SUPERVISOR_ACTOR, True,
                    f"ORPHAN_WATCHER pid={pid} worktree={worktree}",
                    operation="supervisor-recovery",
                    phase="result", result="orphan-watcher",
                )
    chained_audit_log(
        1, "supervisor startup reconciliation", SUPERVISOR_ACTOR, True,
        f"RECOVERY_COMPLETED stale={stale}",
        operation="supervisor-recovery", phase="result", result="completed",
    )
    logger.info("startup reconciliation done (stale=%s)", stale)


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
        "state": "RUNNING",
        "sync_interval": sync_interval,
        "health_interval": health_interval,
        "commit_dirty": commit_dirty,
        "push": push,
        "last_sync": "",
        "last_sync_result": "",
        "sync_cycles": 0,
        "children": [],
    }


def _health_surfaces(root: str | Path) -> dict[str, object]:
    """Extended supervisor health: sync, queue, audit, claims."""
    health: dict[str, object] = {}
    try:
        from .repo_sync import sync_state
        from .merge_queue import MergeQueue
        from . import audit_chain

        state = sync_state(root)
        health["sync"] = {
            "state": state["state"],
            "local_main_sha": state["local_main_sha"],
            "origin_main_sha": state["origin_main_sha"],
        }
        health["merge_queue"] = MergeQueue(root).stats()
        health["audit"] = audit_chain.chain_health()
        try:
            from .claims import ClaimRegistry

            registry = ClaimRegistry(root)
            health["active_claims"] = len(registry.active())
        except Exception:
            health["active_claims"] = "unavailable"
        timeouts_path = _state_dir(root) / "git-timeouts.json"
        if timeouts_path.is_file():
            import json as _json

            try:
                health["timed_out_git_operations"] = _json.loads(
                    timeouts_path.read_text(encoding="utf-8")
                ).get("timed_out_git_operations", 0)
            except (OSError, _json.JSONDecodeError):
                health["timed_out_git_operations"] = "unreadable"
        else:
            health["timed_out_git_operations"] = 0
    except Exception as exc:
        health["health_error"] = type(exc).__name__
    return health


def _deep_health_surfaces(root: str | Path) -> dict[str, object]:
    """DEEP_HEALTH (spec 88): low-frequency, heavier integrity checks.

    Deliberately NOT run in the 20s health cycle: fsck, commit-graph verify,
    multi-pack-index verify, object database domain checks and ref pressure.
    """
    deep: dict[str, object] = {}
    try:
        from .git_repository import GitRepository
        from .git_perf import snapshot as perf_snapshot
        from . import git_cache

        repo = GitRepository(root)
        git_dir = repo.run(["rev-parse", "--git-dir"]).stdout.strip()
        resolved = Path(git_dir)
        if not resolved.is_absolute():
            resolved = Path(root) / resolved
        resolved = resolved.resolve()
        common = repo.run(["rev-parse", "--git-common-dir"]).stdout.strip()
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = Path(root) / common_dir
        common_dir = common_dir.resolve()

        graph_dir = common_dir / "objects" / "info" / "commit-graphs"
        midx = common_dir / "objects" / "pack" / "multi-pack-index"
        deep["commit_graph"] = {
            "exists": graph_dir.is_dir()
            or (common_dir / "objects" / "info" / "commit-graph").exists(),
            "chain_files": len(
                list(graph_dir.glob("graph-*.graph"))
            ) if graph_dir.is_dir() else 0,
        }
        deep["midx_exists"] = midx.is_file()
        fsck = repo.run(["fsck", "--no-dangling"], timeout=120)
        deep["fsck"] = {
            "clean": fsck.returncode == 0,
            "detail": (fsck.stderr or fsck.stdout or "")[:300],
        }
        deep["git_perf"] = perf_snapshot(root)
        deep["ref_pressure"] = _ref_pressure(root, common_dir)
        deep["cache_entries"] = git_cache.stats()
    except Exception as exc:
        deep["deep_error"] = type(exc).__name__
    return deep


def _ref_pressure(root: str | Path, common_dir: Path) -> dict[str, int]:
    """Ref counts per category (spec 96): local / remote / recovery / codex."""
    try:
        from .git_repository import GitRepository

        repo = GitRepository(root)
        refs = repo.run(["for-each-ref", "--format=%(refname)"]).stdout.splitlines()
        counts = {
            "local_branches": sum(r.startswith("refs/heads/") for r in refs),
            "remote_tracking": sum(r.startswith("refs/remotes/") for r in refs),
            "recovery": sum(r.startswith("refs/gptbridge/recovery/") for r in refs),
            "temporary": sum(
                r.startswith(("refs/gptbridge/tmp/", "refs/workers/")) for r in refs
            ),
            "codex": sum(r.startswith("refs/codex/") for r in refs),
            "tags": sum(r.startswith("refs/tags/") for r in refs),
            "total": len(refs),
        }
        return counts
    except Exception:
        return {}


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
    _startup_reconciliation(root, manager, logger)
    registry = _initial_registry(
        sync_interval=sync_interval,
        health_interval=health_interval,
        commit_dirty=commit_dirty,
        push=push,
    )
    watchers: dict[str, _Watcher] = {}
    last_sync_at = time.time()
    last_health_at = 0.0
    last_deep_health_at = 0.0
    logger.info("supervisor started pid=%d root=%s", os.getpid(), root)

    while True:
        try:
            # Graceful shutdown: stop file removal (or foreign lock owner)
            # -> DRAINING: no new merges, finish the current critical
            # section, stop watchers, flush, write final state, exit.
            if not lock_path.exists() or not _lock_is_active_here(lock_path):
                if registry.get("state") != "DRAINING":
                    registry["state"] = "DRAINING"
                    logger.info("stop requested; draining")
            draining = registry.get("state") == "DRAINING"

            if not draining:
                try:
                    assert_write_allowed("automation-supervisor.loop")
                except GovernanceWriteBlocked as exc:
                    if registry.get("governance_write_blocked") != str(exc):
                        logger.warning("governance write blocked: %s", exc)
                    registry["governance_write_blocked"] = str(exc)
                else:
                    registry.pop("governance_write_blocked", None)
                    _refresh_watchers(
                        root, manager, watchers, directory, logger,
                        watch_interval, debounce,
                    )
                    if time.time() - last_sync_at >= sync_interval:
                        last_sync_at = _run_sync_cycle(
                            root, registry, logger,
                            commit_dirty=commit_dirty, push=push,
                        )

            # TODO(inventory): wire health floor (60s) to manifest timings.
            if time.time() - last_health_at >= max(60.0, health_interval * 3):
                registry["health"] = _health_surfaces(root)
                last_health_at = time.time()

            # DEEP_HEALTH (spec 88): heavy integrity checks at low frequency
            # (fsck / commit-graph / midx / ref pressure).  p95 of the 20s
            # health cycle must never run fsck.
            # TODO(inventory): wire deep-health floor (3600s) to manifest.
            if time.time() - last_deep_health_at >= max(3600.0, sync_interval * 60):
                registry["deep_health"] = _deep_health_surfaces(root)
                last_deep_health_at = time.time()

            registry["children"] = [w.to_dict() for w in watchers.values()]
            _write_registry(directory, registry)

            if draining:
                logger.info("draining complete; shutting down")
                break
        except Exception as exc:  # never let the supervisor die silently
            logger.error("supervisor loop error: %s", type(exc).__name__)
        time.sleep(health_interval)

    registry["state"] = "STOPPED"
    registry["stopped_at"] = time.time()
    registry["children"] = []
    try:
        _write_registry(directory, registry)
    except OSError:
        pass
    for watcher in watchers.values():
        _terminate_tree(watcher.process.pid)
    logger.info("supervisor stopped")
