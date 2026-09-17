"""Supervised, persistent automation for the parallel worktree pipeline.

The supervisor is the single long-running owner of the GPTBridge git
automation stack.  It:

  * spawns and supervises one self-commit watcher per registered worktree
    (auto-restart with exponential backoff, per-watcher log files);
  * periodically runs the conflict-safe workspace synchronizer against
    ``main`` so external worker branches are integrated and clean workers
    are fast-forwarded;
  * records its own lifecycle, children, and last sync results in a JSON
    registry under the shared git common directory so any worktree can
    report or stop it;
  * can register itself as a Windows Task Scheduler logon job for
    cross-reboot persistence.

Governance (A53/E39, A58/E44): the supervisor never pushes unless ``--push``
is given (the synchronizer itself guards remote-sync durability), never
force-updates, never resolves conflicts automatically, and never deletes
refs.  All git writes flow through the capability gate
(``execute_system_safe`` → gateway) and are recorded in the tier and
capability audit ledgers.

Sub-modules (A430/E160):
  * ``automation_supervisor_state`` — state dir, registry, stop/status
  * ``automation_supervisor_loop`` — watcher supervision + sync loop
  * ``automation_supervisor_persistence`` — Task Scheduler / Run key
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .automation_supervisor_state import (
    REGISTRY_FILE,
    SUPERVISOR_ACTOR,
    SUPERVISOR_LOCK,
    SUPERVISOR_STATE_SUBDIR,
    _lock_is_active_here,
    _read_registry,
    _state_dir,
    _unlink_with_retry,
    status,
    stop,
)
from .automation_supervisor_loop import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_HEALTH_INTERVAL_SECONDS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_WATCH_INTERVAL_SECONDS,
    _spawn_watcher,
    _Watcher,
    supervise,
    supervise_loop,
)
from .automation_supervisor_persistence import (
    _install_logon,
    _install_task,
    _uninstall_logon,
    _uninstall_task,
)

# Public aliases preserved for existing callers/tests.
state_dir = _state_dir
read_registry = _read_registry
lock_is_active_here = _lock_is_active_here


def start_background(
    root: str | Path,
    *,
    sync_interval: float,
    health_interval: float,
    watch_interval: float,
    debounce: float,
    commit_dirty: bool,
    push: bool,
) -> int:
    """Launch the supervisor as a detached background process (no console)."""
    module = "governance_rule.execution.git_tiers.automation_supervisor"
    cmd = [
        sys.executable,
        "-m",
        module,
        "--root",
        str(root),
        "--foreground",
        "--sync-interval",
        str(int(sync_interval)),
        "--health-interval",
        str(int(health_interval)),
        "--watch-interval",
        str(int(watch_interval)),
        "--debounce",
        str(int(debounce)),
    ]
    if commit_dirty:
        cmd.append("--commit-dirty")
    if push:
        cmd.append("--push")
    handle = subprocess.Popen(  # noqa: S603
        cmd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return handle.pid


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervised persistent automation for parallel worktree sync."
    )
    parser.add_argument("--root", default=os.getcwd(), help="repository root")
    parser.add_argument("--start", action="store_true", help="start as a detached background service")
    parser.add_argument("--foreground", action="store_true", help="run the supervision loop in this process")
    parser.add_argument("--stop", action="store_true", help="stop a running supervisor")
    parser.add_argument("--status", action="store_true", help="report service status")
    parser.add_argument("--install-task", action="store_true", help="register a Task Scheduler logon job (needs elevation)")
    parser.add_argument("--uninstall-task", action="store_true", help="remove the Task Scheduler job")
    parser.add_argument("--install-logon", action="store_true", help="register a per-user logon Run key (no elevation)")
    parser.add_argument("--uninstall-logon", action="store_true", help="remove the per-user logon Run key")
    parser.add_argument("--sync-interval", type=float,
                        default=DEFAULT_SYNC_INTERVAL_SECONDS, help="sync cycle seconds")
    parser.add_argument("--health-interval", type=float,
                        default=DEFAULT_HEALTH_INTERVAL_SECONDS, help="health poll seconds")
    parser.add_argument("--watch-interval", type=float,
                        default=DEFAULT_WATCH_INTERVAL_SECONDS, help="watch poll seconds")
    parser.add_argument("--debounce", type=float,
                        default=DEFAULT_DEBOUNCE_SECONDS, help="commit stability seconds")
    parser.add_argument("--commit-dirty", action="store_true",
                        help="let the sync coordinator commit (use when no watchers)")
    parser.add_argument("--push", action="store_true",
                        help="allow the coordinator to push main after integration")
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point for the automation supervisor service."""
    args = _build_parser().parse_args(argv)

    root = Path(args.root).resolve()
    if args.status:
        print(json.dumps(status(root), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.stop:
        print(stop(root))
        return 0
    if args.install_task:
        return _install_task(root)
    if args.uninstall_task:
        return _uninstall_task()
    if args.install_logon:
        return _install_logon(root)
    if args.uninstall_logon:
        return _uninstall_logon()

    if args.foreground:
        supervise(
            root,
            sync_interval=args.sync_interval,
            health_interval=args.health_interval,
            watch_interval=args.watch_interval,
            debounce=args.debounce,
            commit_dirty=args.commit_dirty,
            push=args.push,
        )
        return 0
    if args.start:
        pid = start_background(
            root,
            sync_interval=args.sync_interval,
            health_interval=args.health_interval,
            watch_interval=args.watch_interval,
            debounce=args.debounce,
            commit_dirty=args.commit_dirty,
            push=args.push,
        )
        print(f"supervisor started (pid {pid})")
        return 0

    parser.error("one of --start/--foreground/--stop/--status is required")
    return 2


__all__ = (
    "REGISTRY_FILE",
    "SUPERVISOR_ACTOR",
    "SUPERVISOR_LOCK",
    "SUPERVISOR_STATE_SUBDIR",
    "cli_main",
    "lock_is_active_here",
    "read_registry",
    "start_background",
    "state_dir",
    "status",
    "stop",
    "supervise",
    "supervise_loop",
)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    raise SystemExit(cli_main())
