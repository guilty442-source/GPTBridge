"""Per-worktree automatic self-commit.

Each git worktree (branch) commits the changes made inside its own checkout
automatically. The single-shot core ``run_once`` is shared by both trigger
modes:

  - watch mode: ``--watch`` polls the worktree every ``interval`` seconds and
    commits once the dirty state has been stable for ``debounce`` seconds;
  - scheduler mode: a scheduler (e.g. Windows Task Scheduler) invokes
    ``--once`` at its own cadence.

Design constraints (A53/E39 + A58/E44 governance):

  * writes go through the capability gate (``execute_system_safe`` issues a
    SYSTEM_SAFE_AUTOMATION Tier-2 token; the gateway executes and audits it)
    and are recorded in the audit ledger with operation ``auto-commit``;
  * only commits — never pushes. Remote sync stays with the coordinator or a
    human;
  * skips while a merge/rebase/cherry-pick/revert is in progress;
  * skips while the index already holds staged-but-uncommitted changes
    (a human/agent is mid-commit; never sweep their index into an
    auto-commit with an unrelated message);
  * skips when the worktree is clean;
  * honours ``.gitignore`` (``git add -A`` never stages ignored files);
  * never amends/rewrites history (tier-3 ops are not invoked).

Usage:
  python -m governance_rule.execution.git_tiers.self_commit \\
      --worktree E:/GPTBridge/.worktrees/ui --watch --interval 30 --debounce 60

  python -m governance_rule.execution.git_tiers.self_commit --once --all

Module layout (A185 source-size split):

    self_commit_lease.py  §10.69-E② commit-lease contract + git-dir helpers
    self_commit_core.py   fingerprint / guards / message / run_once pass
    self_commit.py        cadence constants, watch loop, CLI (this module)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import audit_log  # noqa: F401  (module-level audit alias kept for callers)
from .git_repository import GitRepository
from .governance_manifest import timing as _manifest_timing
from .self_commit_core import (  # noqa: F401  (re-exported core surface)
    _governed,
    _porcelain,
    _run_once_unlocked,
    _state_fingerprint,
    build_commit_message,
    run_once,
    staged_index_present,
)
from .self_commit_lease import (  # noqa: F401  (re-exported lease surface)
    COMMIT_LEASE_FILENAME,
    IN_PROGRESS_MARKERS,
    SELF_COMMIT_ACTOR,
    SELF_COMMIT_LEASE_TTL_SECONDS,
    _commit_lease_path,
    _git_dir_path,
    _is_main_worktree,
    claim_commit_lease,
    commit_lease_active,
    operation_in_progress,
    release_commit_lease,
)
from .worktree_manager import WorktreeManager

# Governed cadence defaults (manifest version source; A318-A320).
DEFAULT_WATCH_INTERVAL_SECONDS: float = _manifest_timing(
    "self_commit_interval_seconds", 30.0
)
DEFAULT_WATCH_DEBOUNCE_SECONDS: float = _manifest_timing(
    "self_commit_debounce_seconds", 60.0
)
DEBOUNCE_FLOOR_SECONDS: float = _manifest_timing(
    "self_commit_base_debounce_seconds", 60.0
)
DEBOUNCE_CEILING_SECONDS: float = _manifest_timing(
    "self_commit_max_debounce_seconds", 300.0
)
ADAPTIVE_WINDOW_SECONDS: float = _manifest_timing(
    "self_commit_adaptive_window_seconds", 60.0
)
ADAPTIVE_BUSY_COMMITS: int = max(
    1, int(_manifest_timing("self_commit_busy_commits_per_window", 4))
)


def watch(
    worktree: str | Path,
    *,
    interval: float = DEFAULT_WATCH_INTERVAL_SECONDS,
    debounce: float = DEFAULT_WATCH_DEBOUNCE_SECONDS,
    actor: str = SELF_COMMIT_ACTOR,
) -> None:
    """Watch a worktree and self-commit stable dirty states."""
    repo = GitRepository(worktree)
    last_state: str = ""
    stable_since: float = 0.0
    commits_in_window: int = 0
    window_started: float = time.monotonic()
    print(f"[self-commit] watching {repo.path} "
          f"(interval={interval}s, debounce={debounce}s)", file=sys.stderr)
    while True:
        try:
            state = _state_fingerprint(repo)
        except PermissionError:
            state = ""
        if state and state != last_state:
            last_state = state
            stable_since = time.monotonic()
        elif state and (time.monotonic() - stable_since) >= debounce:
            status = run_once(repo.path, actor=actor)
            last_state = ""
            window = time.monotonic() - window_started
            # Adaptive debounce (spec 93): under heavy commit activity the
            # debounce grows so watchers don't thundering-herd; under quiet
            # load it shrinks back.  Bounded, never unlimited delay.
            if status == "committed":
                commits_in_window += 1
                if window >= ADAPTIVE_WINDOW_SECONDS:
                    if commits_in_window >= ADAPTIVE_BUSY_COMMITS:  # busy
                        debounce = min(
                            debounce * 1.5, DEBOUNCE_CEILING_SECONDS
                        )
                    elif debounce > DEBOUNCE_FLOOR_SECONDS:  # calm
                        debounce = max(
                            DEBOUNCE_FLOOR_SECONDS, debounce * 0.8
                        )
                    commits_in_window = 0
                    window_started = time.monotonic()
        elif not state:
            last_state = ""
        time.sleep(interval)


def _discover_worktrees(bare_or_main: str | Path) -> list[str]:
    """List every registered worktree path, including the top-level checkout."""
    manager = WorktreeManager(GitRepository(bare_or_main))
    worktrees = [wt["path"] for wt in manager.list_worktrees()]
    requested = str(Path(bare_or_main).resolve())
    normalized = {os.path.normcase(str(Path(item).resolve())) for item in worktrees}
    if os.path.normcase(requested) not in normalized:
        worktrees.insert(0, requested)
    return worktrees


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point for the self-commit service."""
    parser = argparse.ArgumentParser(
        description="Per-worktree automatic self-commit (commits only, never pushes)."
    )
    parser.add_argument(
        "--worktree", help="path of the worktree/main checkout to self-commit"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single commit pass and exit"
    )
    parser.add_argument(
        "--watch", action="store_true", help="keep watching and commit stable changes"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="act on every registered worktree (from this repository)",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_WATCH_INTERVAL_SECONDS,
        help="poll seconds",
    )
    parser.add_argument(
        "--debounce", type=float, default=DEFAULT_WATCH_DEBOUNCE_SECONDS,
        help="stability seconds",
    )
    parser.add_argument(
        "--actor", default=SELF_COMMIT_ACTOR,
        help="audit actor for self-commit operations",
    )
    args = parser.parse_args(argv)

    if args.watch and not args.all and not args.worktree:
        parser.error("--watch requires --worktree <path> or --all")

    if args.all:
        root = args.worktree or os.getcwd()
        targets = _discover_worktrees(root)
    elif args.worktree:
        targets = [args.worktree]
    else:
        targets = [os.getcwd()]

    if args.watch:
        if args.all:
            spawned: list[int] = []
            for target in targets:
                spawned.append(_spawn_watch_subprocess(target, args))
                print(f"[self-commit] spawned watcher for {target} (pid {spawned[-1]})",
                      file=sys.stderr)
            return 0
        watch(targets[0], interval=args.interval, debounce=args.debounce, actor=args.actor)
        return 0

    for target in targets:
        status = run_once(target, actor=args.actor)
        print(f"[self-commit] {target}: {status}", file=sys.stderr)
    return 0


def _spawn_watch_subprocess(target: str, args: argparse.Namespace) -> int:
    """Spawn an independent background watcher for one worktree (no console)."""
    import subprocess

    module = "governance_rule.execution.git_tiers.self_commit"
    cmd = [
        sys.executable, "-m", module,
        "--worktree", target,
        "--watch",
        "--interval", str(args.interval),
        "--debounce", str(args.debounce),
        "--actor", args.actor,
    ]
    handle = subprocess.Popen(  # noqa: S603
        cmd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return handle.pid


if __name__ == "__main__":
    raise SystemExit(cli_main())
