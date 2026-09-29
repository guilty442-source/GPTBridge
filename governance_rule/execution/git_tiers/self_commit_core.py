"""Self-commit single-pass core (A185 split).

Extracted from ``self_commit.py`` (source-size contract): dirty-state
fingerprinting, the staged-index guard, commit-message composition, the
capability-gate adapter and the serialized ``run_once`` pass (with the
§10.69-E② lease dance and transient ``index.lock`` retry).
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import Any

from .audit_chain import chained_audit_log
from .git_repository import GitRepository
from .governance_manifest import (
    GovernanceWriteBlocked,
    assert_write_allowed,
)
from .process_lock import LockBusyError, ProcessFileLock, lock_is_active
from .self_commit_lease import (
    SELF_COMMIT_ACTOR,
    SELF_COMMIT_LEASE_TTL_SECONDS,
    _git_dir_path,
    _is_main_worktree,
    claim_commit_lease,
    commit_lease_active,
    operation_in_progress,
    release_commit_lease,
)


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
        # Git checkouts drop FILE_ATTRIBUTE_READONLY on protected
        # governance sources; restore the invariant before the commit
        # audit so attribute loss alone never blocks the sweep.  The
        # audit remains the authority if a restore genuinely fails.
        try:
            from .protected_attrs import restore_protected_readonly

            restore_protected_readonly(repo.path)
        except Exception:
            pass
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
        # Transient index.lock contention: concurrent workers/agents hold
        # the shared index for seconds at a time (all worktrees share one
        # .git).  Retry the add within a short bounded window instead of
        # failing the whole sweep; any other failure mode returns at once.
        for _ in range(3):
            if (
                add_result is None
                or add_result.returncode == 0
                or "index.lock" not in str(add_result.stderr)
            ):
                break
            time.sleep(3.0)
            gate = _governed(repo, ["add", "-A"], actor=actor)
            add_result = gate.execution_result
        if gate.allowed is False or add_result is None or add_result.returncode != 0:
            if gate.allowed is False:
                detail = gate.detail
            else:
                stderr = str(add_result.stderr or "").strip()
                # Prefer the fatal/error tail: `git add` emits CRLF warnings
                # first, and a head-truncated stderr hides the real cause.
                fatal = [
                    line for line in stderr.splitlines()
                    if line.lstrip().startswith(("fatal:", "error:"))
                ]
                detail = "; ".join(fatal)[:200] if fatal else stderr[:200]
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


__all__ = [
    "build_commit_message", "run_once", "staged_index_present",
    "_governed", "_porcelain", "_run_once_unlocked", "_state_fingerprint",
]
