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

from .branch_policy import MAIN_BRANCH, is_main, normalize_branch
from .git_repository import GitRepository
from .governance_manifest import (
    GovernanceWriteBlocked,
    assert_write_allowed,
    timing as _manifest_timing,
)
from .merge_precheck import pre_merge_check
from .merge_queue import MergeQueue
from .process_lock import LockBusyError, ProcessFileLock
from .recovery import create_recovery_ref
from .release_checkpoint import record_checkpoint
from .self_commit import run_once
from .worktree_manager import WorktreeManager

SYNC_ACTOR = "governance/workspace-sync"
DEFAULT_SYNC_INTERVAL_SECONDS: float = _manifest_timing(
    "workspace_sync_interval_seconds", 60.0
)


def _is_worker_branch(branch: str) -> bool:
    """A branch the synchronizer may integrate / fast-forward (not main/HEAD)."""
    return bool(branch) and branch != "HEAD" and not is_main(branch)


def _common_git_dir(repo: GitRepository) -> Path:
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    path = Path(raw)
    return (path if path.is_absolute() else repo.path / path).resolve()


def _governed(repo: GitRepository, args: list[str], *, timeout: float | None = None):
    """Execute one integration write through the capability gate."""
    from .capability_gate import execute_system_safe

    return execute_system_safe(
        list(args), actor=SYNC_ACTOR, repo_path=repo.path, timeout=timeout,
    )


def _gate_failed(gate) -> bool:
    result = gate.execution_result
    return gate.allowed is False or result is None or result.returncode != 0


def _gate_error(gate) -> str:
    if gate.allowed is False:
        return str(gate.detail)[:160]
    result = gate.execution_result
    if result is None:
        return "no-execution-result"
    return str(result.stderr or result.stdout or "").strip()[:160]


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
    try:
        assert_write_allowed("workspace-sync.synchronize")
    except GovernanceWriteBlocked as exc:
        return f"error:{exc}"
    coordinator = GitRepository(root)
    manager = WorktreeManager(coordinator)
    worktrees = manager.list_worktrees()
    main = next(
        (item for item in worktrees if is_main(item.get("branch", ""))),
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
            branch = normalize_branch(item.get("branch", ""))
            if not _is_worker_branch(branch):
                continue
            checked = coordinator.run(["diff", "--check", f"{MAIN_BRANCH}...{branch}"])
            if checked.returncode != 0:
                invalid_diffs.append(branch)
        if invalid_diffs:
            return "error:worker-diff-check:" + "|".join(invalid_diffs)

        main_repo = GitRepository(main["path"])
        queue = MergeQueue(root)
        merged_any = False
        for item in worktrees:
            branch = normalize_branch(item.get("branch", ""))
            if not _is_worker_branch(branch):
                continue
            ancestor = main_repo.run(["merge-base", "--is-ancestor", branch, MAIN_BRANCH])
            if ancestor.returncode == 0:
                continue
            worker_id = Path(item["path"]).name
            source_sha = main_repo.run(["rev-parse", branch]).stdout.strip()
            base_sha = main_repo.run(["rev-parse", MAIN_BRANCH]).stdout.strip()
            existing = queue.find_entry(branch, source_sha)
            if existing and existing.get("status") in {"conflicted", "blocked", "failed"}:
                return f"conflict:{branch}:retry-blocked"
            entry = existing or queue.enqueue(
                worker_id,
                branch,
                source_sha,
                base_main_commit=base_sha,
                escalated_by=SYNC_ACTOR,
            )
            if entry.get("status") == "error":
                return f"error:merge-queue:{entry.get('detail', 'enqueue-failed')}"
            queue.mark_running(entry["queue_id"])

            precheck = pre_merge_check(main_repo, source_sha, target=MAIN_BRANCH)
            if not precheck["has_ancestor"]:
                queue.mark_blocked(entry["queue_id"], "no-common-ancestor")
                return f"conflict:{branch}:no-merge-base"
            if not precheck["diff_check_clean"]:
                queue.mark_conflicted(entry["queue_id"], "diff-check-failed")
                return f"conflict:{branch}:diff-check"

            recovery_ref = create_recovery_ref(
                main_repo, entry["queue_id"], target=MAIN_BRANCH, actor=SYNC_ACTOR
            )
            merged = _governed(main_repo, ["merge", "--no-edit", source_sha])
            if _gate_failed(merged):
                _governed(main_repo, ["merge", "--abort"])
                queue.mark_conflicted(
                    entry["queue_id"], f"merge-failed; recovery={recovery_ref}"
                )
                return f"conflict:{branch}"
            merged_any = True
            queue.mark_merged(
                entry["queue_id"], f"merged {source_sha} into main; recovery={recovery_ref}"
            )

        if not _audit_passes(main["path"]):
            return "error:integrated-main-governance-audit"

        if merged_any:
            record_checkpoint(
                main["path"], audit_result="pass", actor=SYNC_ACTOR
            )

        for item in worktrees:
            branch = normalize_branch(item.get("branch", ""))
            if not _is_worker_branch(branch):
                continue
            repo = GitRepository(item["path"])
            advanced = _governed(repo, ["merge", "--ff-only", MAIN_BRANCH])
            if _gate_failed(advanced):
                return (
                    f"error:fast-forward:{branch}:{_gate_error(advanced)}"
                )
        if push:
            fetched = _governed(
                main_repo, ["fetch", "origin", MAIN_BRANCH]
            )
            if _gate_failed(fetched):
                return "error:fetch-origin-main"
            remote_is_ancestor = main_repo.run(
                ["merge-base", "--is-ancestor", f"origin/{MAIN_BRANCH}", MAIN_BRANCH]
            )
            if remote_is_ancestor.returncode != 0:
                return "error:remote-main-diverged"
            pushed = _governed(main_repo, ["push", "origin", MAIN_BRANCH])
            if _gate_failed(pushed):
                return f"error:push:{_gate_error(pushed)}"
    return "synchronized-and-pushed" if push else "synchronized"


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize all GPTBridge worktrees safely.")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_SYNC_INTERVAL_SECONDS
    )
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
