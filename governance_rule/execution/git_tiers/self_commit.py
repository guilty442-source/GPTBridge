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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import audit_log
from .audit_chain import chained_audit_log
from .git_repository import GitRepository
from .governance_manifest import (
    GovernanceWriteBlocked,
    assert_write_allowed,
    timing as _manifest_timing,
)
from .process_lock import LockBusyError, ProcessFileLock, lock_is_active
from .worktree_manager import WorktreeManager

SELF_COMMIT_ACTOR: str = "governance/self-commit"
IN_PROGRESS_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)

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


def _git_dir_path(repo: GitRepository) -> Path:
    result = repo.run(["rev-parse", "--git-dir"])
    raw = (result.stdout or "").strip()
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = repo.path / resolved
    return resolved.resolve()


def _is_main_worktree(repo: GitRepository) -> bool:
    """Return True for the primary checkout, which Git does not allow locking."""
    common = repo.run(["rev-parse", "--git-common-dir"])
    raw = (common.stdout or "").strip()
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = repo.path / common_dir
    return _git_dir_path(repo) == common_dir.resolve()


def operation_in_progress(repo: GitRepository) -> bool:
    """True when a merge/rebase/cherry-pick/revert is underway (or sequencer)."""
    git_dir = _git_dir_path(repo)
    if any((git_dir / marker).exists() for marker in IN_PROGRESS_MARKERS):
        return True
    return bool((git_dir / "sequencer").exists())


# --- §10.69-E② single-writer commit lease -------------------------------
# A human/agent declares an in-progress commit sequence by writing a lease
# file inside .git; the self-commit sweep defers while any fresh lease is
# held, and declares its own lease for the add+commit window so
# lease-aware writers yield both ways. Git itself never sees the file —
# this is a cooperating-writers contract, not a lock (HEAD can still move;
# writers must be prepared to retry ``cannot lock ref HEAD``).

COMMIT_LEASE_FILENAME = "self-commit-lease.json"
SELF_COMMIT_LEASE_TTL_SECONDS = 120.0


def _commit_lease_path(repo: GitRepository) -> Path:
    return _git_dir_path(repo) / COMMIT_LEASE_FILENAME


def commit_lease_active(repo: GitRepository) -> dict | None:
    """Fresh lease data when another writer is mid-commit, else None."""
    try:
        data = json.loads(
            _commit_lease_path(repo).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None
    try:
        if float(data.get("until") or 0) > time.time():
            return data
    except (TypeError, ValueError):
        pass
    return None


def claim_commit_lease(
    worktree: str | Path, *, owner: str, ttl_seconds: float = 300.0
) -> Path | None:
    """Declare an in-progress commit; renew before ttl, release when done.

    Workers/agents call this BEFORE ``git add`` so the self-commit sweep
    never stages their in-flight work under a generic message. Returns
    None when a fresh lease from another owner already exists — callers
    must defer instead of overwriting (the lease is a cooperating-writers
    contract, not a lock)."""
    repo = GitRepository(worktree)
    path = _commit_lease_path(repo)
    existing = commit_lease_active(repo)
    if existing is not None and existing.get("owner") != str(owner):
        return None
    path.write_text(
        json.dumps(
            {
                "owner": str(owner),
                "claimed_at": time.time(),
                "until": time.time() + max(1.0, float(ttl_seconds)),
            }
        ),
        encoding="utf-8",
    )
    return path


def release_commit_lease(worktree: str | Path, *, owner: str) -> None:
    """Release only the caller's own lease — never delete another
    writer's claim (a racing claim that landed after ours stays valid)."""
    repo = GitRepository(worktree)
    try:
        data = json.loads(
            _commit_lease_path(repo).read_text(encoding="utf-8")
        )
        if data.get("owner") != str(owner):
            return
        _commit_lease_path(repo).unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def _porcelain(repo: GitRepository) -> dict[str, str]:
    """Map of {status: path} from porcelain v2 -z (respects ignore)."""
    from .porcelain import status_v2

    return status_v2(repo, include_branch=False).legacy_map()


def _state_fingerprint(repo: GitRepository) -> str:
    """Stable fingerprint of the worktree's dirty state.

    X6: one ``diff --numstat HEAD`` covers staged + unstaged churn in a
    single subprocess (the fingerprint only needs change detection, not
    the staged/unstaged split).
    """
    entries = _porcelain(repo)
    numstat = repo.run(["diff", "--numstat", "HEAD"]).stdout
    payload = repr(sorted(entries.items())) + numstat
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def staged_index_present(repo: GitRepository) -> bool:
    """True when the index holds staged-but-uncommitted changes.

    A pre-existing index means a human or coding agent is mid-commit.  The
    self-commit service must not sweep those entries into an automatic commit
    with an unrelated message (incident 2026-09-20: ef58c9cc carried
    externally staged P0 work under a blueprint commit message).
    """
    result = repo.run(["diff", "--cached", "--quiet"])
    return result.returncode == 1


def build_commit_message(branch: str, entries: dict[str, str]) -> str:
    """Compose an auto-commit message from porcelain entries.

    G101/§3.4: the subject names the affected scopes so the semantic
    batch is identifiable without expanding the body."""
    from .generation_snapshot import scope_of

    count = len(entries)
    scopes = sorted({scope_of(p) for p in entries})
    scope_tag = f" [{', '.join(scopes[:4])}]" if scopes else ""
    subject = f"auto-commit({branch}): {count} file(s) updated{scope_tag}"
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


def _governed(
    repo: GitRepository, args: list[str], *, actor: str
):
    """Execute one Tier-2 automation command through the capability gate."""
    from .capability_gate import execute_system_safe

    return execute_system_safe(args, actor=actor, repo_path=repo.path)


def _run_once_unlocked(
    worktree: str | Path,
    *,
    actor: str = SELF_COMMIT_ACTOR,
    snapshot: Any | None = None,
) -> str:
    """Perform one self-commit pass; returns a short status string.

    Statuses: ``clean``, ``in-progress``, ``staged-index-present``,
    ``committed``, ``nothing-staged``, ``error:<detail>``.
    """
    repo = GitRepository(worktree)
    if not repo.path.is_dir():
        return "error:not-a-directory"

    if operation_in_progress(repo):
        return "in-progress"

    if staged_index_present(repo):
        return "staged-index-present"

    lease = commit_lease_active(repo)
    if lease is not None and lease.get("owner") != actor:
        return f"commit-lease-held:{lease.get('owner')}"

    # G101: reuse the caller's shared generation snapshot when provided —
    # one capture per cycle instead of a second status subprocess.
    entries = (
        dict(snapshot.status_map)
        if snapshot is not None
        else _porcelain(repo)
    )
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
        if claim_commit_lease(
            repo.path, owner=actor, ttl_seconds=SELF_COMMIT_LEASE_TTL_SECONDS
        ) is None:
            lease = commit_lease_active(repo) or {}
            return f"commit-lease-held:{lease.get('owner')}"
        if not _is_main_worktree(repo):
            gate = _governed(
                repo, ["worktree", "lock", str(repo.path)], actor=actor
            )
            lock_result = gate.execution_result
            if (
                gate.allowed is False
                or lock_result is None
                or lock_result.returncode != 0
            ):
                detail = (
                    gate.detail if gate.allowed is False
                    else str(lock_result.stderr).strip()[:200]
                )
                return f"error:lock:{detail}"
            locked = True
        gate = _governed(repo, ["add", "-A"], actor=actor)
        add_result = gate.execution_result
        if gate.allowed is False or add_result is None or add_result.returncode != 0:
            detail = (
                gate.detail if gate.allowed is False
                else str(add_result.stderr).strip()[:200]
            )
            return f"error:add:{detail}"
        if not _porcelain(repo):
            return "nothing-staged"
        gate = _governed(repo, ["commit", "-F", str(msg_file)], actor=actor)
        commit_result = gate.execution_result
        if (
            gate.allowed is False
            or commit_result is None
            or commit_result.returncode != 0
        ):
            detail = (
                gate.detail if gate.allowed is False
                else str(commit_result.stderr).strip()[:500]
            )
            chained_audit_log(
                2,
                "auto-commit fail",
                actor,
                True,
                detail,
                operation="auto-commit",
                phase="result",
                result="failed",
                returncode=(
                    commit_result.returncode if commit_result is not None else -1
                ),
            )
            # Unstage what this pass staged — otherwise the leftover index
            # makes every later sweep report staged-index-present forever
            # (the mid-commit guard deadlocks on the sweep's own debris).
            # Worktree files are untouched; only the index is cleared.
            # `restore` is a SYSTEM_SAFE_TIER2 op; `reset` is gate-denied.
            try:
                _governed(repo, ["restore", "--staged", "."], actor=actor)
            except Exception:
                pass
            return f"error:commit:{detail[:200]}"
    finally:
        if locked:
            _governed(repo, ["worktree", "unlock", str(repo.path)], actor=actor)
        try:
            msg_file.unlink(missing_ok=True)
        except OSError:
            pass
        release_commit_lease(repo.path, owner=actor)

    commit_hash = repo.head()
    # G101 event invalidation: the commit changed this worktree's
    # generation — cached snapshots must recapture on next read.
    from .generation_snapshot import notify_changed

    notify_changed(repo.path)
    chained_audit_log(
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


def run_once(
    worktree: str | Path,
    *,
    actor: str = SELF_COMMIT_ACTOR,
    snapshot: Any | None = None,
) -> str:
    """Serialize commits and yield while the workspace coordinator is active.

    ``snapshot`` (G101): a shared ``GenerationSnapshot`` captured by the
    caller — its ``status_map`` replaces the initial porcelain read; the
    mid-commit freshness recheck still runs its own status."""
    try:
        assert_write_allowed("self-commit.run_once")
    except GovernanceWriteBlocked as exc:
        return f"error:{exc}"
    repo = GitRepository(worktree)
    common_result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (common_result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    common = common.resolve()
    coordinator = common / "gptbridge-workspace-sync.lock"
    if actor != "governance/workspace-sync" and lock_is_active(coordinator):
        return "in-progress"
    key = hashlib.sha256(str(repo.path).casefold().encode("utf-8")).hexdigest()[:16]
    try:
        with ProcessFileLock(common / f"gptbridge-self-commit-{key}.lock"):
            return _run_once_unlocked(
                worktree, actor=actor, snapshot=snapshot
            )
    except LockBusyError:
        return "in-progress"


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
