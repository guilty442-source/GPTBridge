"""Conflict-safe automatic synchronization for GPTBridge worktrees.

Each checkout commits only its own files. The coordinator then merges worker
branches into ``main``, audits the integrated result, fast-forwards every clean
worker checkout, and can push ``main``. It never force-updates, resets, deletes
refs, or resolves conflicts automatically.
"""
from __future__ import annotations

import argparse
import os
import subprocess
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


def _audit_passes(path: str | Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "governance_rule.execution.audit"],
        cwd=Path(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


def synchronize(
    root: str | Path, *, commit_dirty: bool = True, push: bool = False
) -> str:
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

        invalid_diffs: list[str] = []
        for item in worktrees:
            branch = _branch_name(item.get("branch", ""))
            if not branch or branch in {"HEAD", "main"}:
                continue
            checked = coordinator.run(["diff", "--check", f"main...{branch}"])
            if checked.returncode != 0:
                invalid_diffs.append(branch)
        if invalid_diffs:
            return "error:worker-diff-check:" + "|".join(invalid_diffs)

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

        if not _audit_passes(main["path"]):
            return "error:integrated-main-governance-audit"

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
        if push:
            fetched = main_repo.run(
                ["fetch", "origin", "main"], confirmed=True, actor=SYNC_ACTOR
            )
            if fetched.returncode != 0:
                return "error:fetch-origin-main"
            remote_is_ancestor = main_repo.run(
                ["merge-base", "--is-ancestor", "origin/main", "main"]
            )
            if remote_is_ancestor.returncode != 0:
                return "error:remote-main-diverged"
            pushed = main_repo.run(
                ["push", "origin", "main"], confirmed=True, actor=SYNC_ACTOR
            )
            if pushed.returncode != 0:
                return f"error:push:{pushed.stderr.strip()[:160]}"
    return "synchronized-and-pushed" if push else "synchronized"


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize all GPTBridge worktrees safely.")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args(argv)
    while True:
        try:
            result = synchronize(
                args.root, commit_dirty=not args.no_commit, push=args.push
            )
        except LockBusyError as exc:
            result = f"error:{exc}"
        except Exception as exc:  # keep watch mode alive without leaking details
            result = f"error:unexpected:{type(exc).__name__}"
        print(f"[workspace-sync] {result}", file=sys.stderr)
        if not args.watch:
            return 0 if result in {"synchronized", "synchronized-and-pushed"} else 1
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(cli_main())
