"""Conflict-safe automatic synchronization for GPTBridge worktrees.

Each checkout commits only its own files. The coordinator then merges worker
branches into ``main`` and fast-forwards every clean worker checkout. It never
pushes, force-updates, resets, or resolves conflicts automatically.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .git_repository import GitRepository
from .process_lock import LockBusyError, ProcessFileLock
from .self_commit import run_once
from .worktree_manager import WorktreeManager

SYNC_ACTOR = "governance/workspace-sync"


def _branch_name(value: str) -> str:
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else value


def _common_git_dir(repo: GitRepository) -> Path:
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    path = Path(raw)
    return (path if path.is_absolute() else repo.path / path).resolve()


def synchronize(root: str | Path, *, commit_dirty: bool = True) -> str:
    """Commit, integrate, and fast-forward all registered worktrees."""
    coordinator = GitRepository(root)
    manager = WorktreeManager(coordinator)
    worktrees = manager.list_worktrees()
    main = next(
        (item for item in worktrees if _branch_name(item.get("branch", "")) == "main"),
        None,
    )
    if main is None:
        return "error:main-worktree-not-found"

    lock_path = _common_git_dir(coordinator) / "gptbridge-workspace-sync.lock"
    with ProcessFileLock(lock_path):
        if commit_dirty:
            for item in worktrees:
                result = run_once(item["path"], actor=SYNC_ACTOR)
                if result.startswith("error:") or result == "in-progress":
                    return f"error:self-commit:{item['path']}:{result}"

        worktrees = manager.list_worktrees()
        dirty = [item["path"] for item in worktrees if GitRepository(item["path"]).status()]
        if dirty:
            return "error:dirty-worktree:" + "|".join(dirty)

        main_repo = GitRepository(main["path"])
        for item in worktrees:
            branch = _branch_name(item.get("branch", ""))
            if not branch or branch in {"HEAD", "main"}:
                continue
            ancestor = main_repo.run(["merge-base", "--is-ancestor", branch, "main"])
            if ancestor.returncode == 0:
                continue
            merged = main_repo.run(
                ["merge", "--no-edit", branch],
                confirmed=True,
                actor=SYNC_ACTOR,
            )
            if merged.returncode != 0:
                main_repo.run(["merge", "--abort"], confirmed=True, actor=SYNC_ACTOR)
                return f"conflict:{branch}"

        for item in worktrees:
            branch = _branch_name(item.get("branch", ""))
            if not branch or branch in {"HEAD", "main"}:
                continue
            repo = GitRepository(item["path"])
            advanced = repo.run(
                ["merge", "--ff-only", "main"],
                confirmed=True,
                actor=SYNC_ACTOR,
            )
            if advanced.returncode != 0:
                return f"error:fast-forward:{branch}:{advanced.stderr.strip()[:160]}"
    return "synchronized"


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize all GPTBridge worktrees safely.")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--no-commit", action="store_true")
    args = parser.parse_args(argv)
    while True:
        try:
            result = synchronize(args.root, commit_dirty=not args.no_commit)
        except LockBusyError as exc:
            result = f"error:{exc}"
        except Exception as exc:  # keep watch mode alive without leaking details
            result = f"error:unexpected:{type(exc).__name__}"
        print(f"[workspace-sync] {result}", file=sys.stderr)
        if not args.watch:
            return 0 if result == "synchronized" else 1
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(cli_main())
