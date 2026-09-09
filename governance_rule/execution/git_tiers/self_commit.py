"""Per-worktree automatic self-commit.

Each git worktree (branch) commits the changes made inside its own checkout
automatically. The single-shot core ``run_once`` is shared by both trigger
modes:

  - watch mode: ``--watch`` polls the worktree every ``interval`` seconds and
    commits once the dirty state has been stable for ``debounce`` seconds;
  - scheduler mode: a scheduler (e.g. Windows Task Scheduler) invokes
    ``--once`` at its own cadence.

Design constraints (A53/E39 + A58/E44 governance):

  * writes go through ``GitRepository.run`` with ``confirmed=True`` and are
    recorded in the audit ledger with operation ``auto-commit``;
  * only commits — never pushes. Remote sync stays with the coordinator or a
    human;
  * skips while a merge/rebase/cherry-pick/revert is in progress;
  * skips when the worktree is clean;
  * honours ``.gitignore`` (``git add -A`` never stages ignored files);
  * never amends/rewrites history (tier-3 ops are not invoked).

Usage:
  python -m governance_rule.execution.git_tiers.self_commit \\
      --worktree E:/GPTBridge-worktrees/ui --watch --interval 30 --debounce 60

  python -m governance_rule.execution.git_tiers.self_commit --once --all
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

from . import audit_log
from .git_repository import GitRepository
from .worktree_manager import WorktreeManager

SELF_COMMIT_ACTOR: str = "governance/self-commit"
IN_PROGRESS_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


def _git_dir_path(repo: GitRepository) -> Path:
    result = repo.run(["rev-parse", "--git-dir"])
    raw = (result.stdout or "").strip()
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = repo.path / resolved
    return resolved.resolve()


def operation_in_progress(repo: GitRepository) -> bool:
    """True when a merge/rebase/cherry-pick/revert is underway (or sequencer)."""
    git_dir = _git_dir_path(repo)
    if any((git_dir / marker).exists() for marker in IN_PROGRESS_MARKERS):
        return True
    return bool((git_dir / "sequencer").exists())


def _porcelain(repo: GitRepository) -> dict[str, str]:
    """Map of {status: path} from `git status --porcelain` (respects ignore)."""
    result = repo.run(["status", "--porcelain"])
    entries: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if len(line) < 4:
            continue
        entries[line[3:].strip()] = line[:2].strip()
    return entries


def _state_fingerprint(repo: GitRepository) -> str:
    """Stable fingerprint of the worktree's dirty state."""
    entries = _porcelain(repo)
    staged = repo.run(["diff", "--cached"]).stdout
    unstaged = repo.run(["diff"]).stdout
    payload = repr(sorted(entries.items())) + staged + unstaged
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def build_commit_message(branch: str, entries: dict[str, str]) -> str:
    """Compose an auto-commit message from porcelain entries."""
    count = len(entries)
    subject = f"auto-commit({branch}): {count} file(s) updated"
    body: list[str] = ["", "Automated self-commit by GPTBridge governance."]
    for status, path in sorted(entries.items()):
        body.append(f"- [{status}] {path}")
    body.extend(
        [
            "",
            "Generated with [GPTBridge](https://github.com/guilty442-source/GPTBridge)",
            "",
            "Co-Authored-By: GPTBridge Self-Commit <governance@gptbridge.local>",
        ]
    )
    return subject + "\n" + "\n".join(body) + "\n"


def run_once(worktree: str | Path, *, actor: str = SELF_COMMIT_ACTOR) -> str:
    """Perform one self-commit pass; returns a short status string.

    Statuses: ``clean``, ``in-progress``, ``committed``, ``nothing-staged``,
    ``error:<detail>``.
    """
    repo = GitRepository(worktree)
    if not repo.path.is_dir():
        return "error:not-a-directory"

    if operation_in_progress(repo):
        return "in-progress"

    entries = _porcelain(repo)
    if not entries:
        return "clean"

    identity = repo.run(["config", "--get", "user.name"])
    if not (identity.stdout or "").strip():
        return "error:missing-identity"

    branch = repo.current_branch() or "HEAD"
    message = build_commit_message(branch, entries)

    git_dir = _git_dir_path(repo)
    msg_file = git_dir / "self-commit-msg.txt"
    try:
        msg_file.write_text(message, encoding="utf-8")
    except OSError as exc:
        return f"error:write-msg:{exc}"

    locked = False
    try:
        lock_result = repo.run(
            ["worktree", "lock", str(repo.path)],
            confirmed=True,
            actor=actor,
        )
        if lock_result.returncode != 0:
            return f"error:lock:{lock_result.stderr.strip()[:200]}"
        locked = True
        add_result = repo.run(["add", "-A"], confirmed=True, actor=actor)
        if add_result.returncode != 0:
            return f"error:add:{add_result.stderr.strip()[:200]}"
        if not _porcelain(repo):
            return "nothing-staged"
        commit_result = repo.run(
            ["commit", "-F", str(msg_file)],
            confirmed=True,
            actor=actor,
        )
        if commit_result.returncode != 0:
            audit_log(
                2,
                "auto-commit fail",
                actor,
                True,
                commit_result.stderr.strip()[:500],
                operation="auto-commit",
                phase="result",
                result="failed",
                returncode=commit_result.returncode,
            )
            return f"error:commit:{commit_result.stderr.strip()[:200]}"
    finally:
        if locked:
            repo.run(
                ["worktree", "unlock", str(repo.path)],
                confirmed=True,
                actor=actor,
            )
        try:
            msg_file.unlink(missing_ok=True)
        except OSError:
            pass

    commit_hash = repo.head()
    audit_log(
        2,
        "auto-commit",
        actor,
        True,
        f"committed {commit_hash} on {branch}: {len(entries)} file(s)",
        operation="auto-commit",
        phase="result",
        result="succeeded",
    )
    print(f"[self-commit] {repo.path}: {commit_hash} on {branch} "
          f"({len(entries)} file(s))", file=sys.stderr)
    return "committed"


def watch(
    worktree: str | Path,
    *,
    interval: float = 30.0,
    debounce: float = 60.0,
    actor: str = SELF_COMMIT_ACTOR,
) -> None:
    """Watch a worktree and self-commit stable dirty states."""
    repo = GitRepository(worktree)
    last_state: str = ""
    stable_since: float = 0.0
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
            run_once(repo.path, actor=actor)
            last_state = ""
        elif not state:
            last_state = ""
        time.sleep(interval)


def _discover_worktrees(bare_or_main: str | Path) -> list[str]:
    """List every registered worktree path, including the top-level checkout."""
    manager = WorktreeManager(GitRepository(bare_or_main))
    worktrees = [wt["path"] for wt in manager.list_worktrees()]
    if str(bare_or_main) not in worktrees:
        worktrees.insert(0, str(bare_or_main))
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
    parser.add_argument("--interval", type=float, default=30.0, help="poll seconds")
    parser.add_argument("--debounce", type=float, default=60.0, help="stability seconds")
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