"""Git operation tier governance — implements A53/E39.

Three-tier classification:
  Tier 1: read-only, high-frequency, direct execution
  Tier 2: general write, requires confirmation
  Tier 3: high-risk, strictly restricted, requires codex approval

Enforcement: hook + governance gate + audit ledger (A46).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[3]
AUDIT_LEDGER_PATH = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "git_tier_audit.jsonl"
_AUDIT_LOCK: Final[threading.Lock] = threading.Lock()
_EMPTY_HASH: Final[str] = "0" * 64

# Tier operation lists.
#
# A53/E39 declares the canonical TIER-1/2/3 operation sets.  The lists below
# extend those sets with additional operations, classified per A16 (codex is
# its own interpreter) and A11 (fail-closed — when uncertain, classify higher):
#
# TIER-1 extensions (all read-only, safe to direct-exec):
#   worktree list/status, reflog show, fsck, count-objects, shortlog,
#   annotate, name-rev, rev-list, ls-tree, ls-remote, merge-base — these are
#   read-only inspection commands consistent with the TIER-1 spirit (read-only
#   + high-frequency + direct-exec).
#
# TIER-2 extensions (general-write, requires confirmation):
#   stash push/pop/apply, branch create, tag -a/-m, worktree lock/unlock/
#   move/prune, mv, restore, switch -c — standard write operations consistent
#   with TIER-2 spirit.
#   pull, clone — these involve NETWORK access.  Per A58/E44 (governed-network-
#   access), network operations require explicit-capability + static-allowlist
#   + timeout + audit.  They are classified TIER-2 (requires confirmation) but
#   callers MUST additionally ensure the network path is governed.  `clone`
#   especially risks introducing unverified code (A37 code-origin: dependencies
#   must be approved+inventoried+pinned+license-reviewed+security-reviewed).
#
# TIER-3 extensions (high-risk, strictly restricted):
#   push -f/push +, branch -d (conservative — A53 lists only branch -D),
#   commit --amend (conservative — A53 specifies "amend-pushed"; we classify
#   ALL amend as TIER-3 per A11 fail-closed), gc --aggressive, gc --prune=now,
#   reflog expire --expire=now, push --delete, tag -d, replace, notes remove —
#   all destructive or history-rewriting, consistent with TIER-3 spirit.

TIER1_OPS: Final[frozenset[str]] = frozenset({
    "--version", "status", "log", "diff", "show", "branch", "remote", "blame",
    "ls-files", "cat-file", "rev-parse", "describe", "tag -l",
    "for-each-ref", "stash list", "config --get", "config --list",
    "worktree list", "worktree list --porcelain", "worktree status",
    "worktree prune --dry-run", "reflog show", "fsck", "count-objects", "shortlog",
    "annotate", "name-rev", "rev-list", "ls-tree", "ls-remote", "merge-base",
})

TIER2_OPS: Final[frozenset[str]] = frozenset({
    "add", "commit", "stash", "stash push", "stash pop", "stash apply",
    "branch create", "checkout", "switch", "merge", "tag create", "tag -a",
    "tag -m", "fetch", "push", "rebase", "cherry-pick", "revert",
    "worktree add", "worktree create", "worktree lock", "worktree unlock",
    "worktree remove", "worktree move", "worktree prune", "mv", "restore",
    "switch -c", "pull", "clone",
})

TIER3_OPS: Final[frozenset[str]] = frozenset({
    "push --force", "push --force-with-lease", "push -f", "push +",
    "commit --amend", "reset --hard", "reset --soft", "branch -D",
    "branch -d", "branch --delete", "filter-branch", "filter-repo",
    "rebase -i", "rebase --interactive", "rebase --root",
    "gc --prune", "gc --prune=now", "gc --aggressive",
    "reflog expire", "reflog expire --expire=now",
    "update-ref -d", "clean -fd", "clean -fdx", "stash drop",
    "stash clear", "push --delete", "tag -d", "replace", "notes remove",
})


def classify(command: str) -> int:
    """Classify a git command string into tier 1, 2, or 3.

    Returns 1, 2, or 3. Unknown commands fail closed strictly as Tier 3.
    """
    cmd = command.strip().lower()

    # Remove leading "git " if present
    if cmd.startswith("git "):
        cmd = cmd[4:]

    # Check Tier 3 first (most restrictive)
    for op in TIER3_OPS:
        if op.casefold() in cmd:
            return 3

    # Check Tier 1 (read-only)
    for op in TIER1_OPS:
        if cmd.startswith(op) or cmd == op:
            return 1

    # Check Tier 2 (general write)
    for op in TIER2_OPS:
        if cmd.startswith(op) or cmd == op:
            return 2

    # Unknown operations are unverified and therefore require Tier 3 approval.
    return 3


def audit_log(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    *,
    operation: str = "",
    repo_snapshot: dict[str, object] | None = None,
    phase: str = "decision",
    result: str = "pending",
    returncode: int | None = None,
) -> dict[str, object]:
    """Write an audit ledger entry (A46 compliance)."""
    from .snapshot import _capture_repo_snapshot

    snapshot = repo_snapshot if repo_snapshot is not None else _capture_repo_snapshot()
    if not operation:
        operation = command.strip().split()[0] if command.strip() else "unknown"
    AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, object] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "tier": tier,
        "operation": operation,
        "command": command,
        "actor": actor,
        "approved": approved,
        "phase": phase,
        "result": result,
        "returncode": returncode,
        "detail": detail,
        "head_revision": snapshot.get("head_revision", ""),
        "branch": snapshot.get("branch", "HEAD"),
        "dirty_files": snapshot.get("dirty_files", []),
        "staged_files": snapshot.get("staged_files", []),
        "untracked_files": snapshot.get("untracked_files", []),
    }
    with _AUDIT_LOCK, AUDIT_LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    return entry


def enforce(
    command: str,
    actor: str = "unknown",
    *,
    confirmed: bool | None = None,
    authority_approved: bool | None = None,
    repo_snapshot: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """Authorize one classified Git operation and record the decision."""
    tier = classify(command)
    if confirmed is None:
        confirmed = os.environ.get("GOVERNANCE_CONFIRM", "").casefold() in {
            "1", "true", "yes",
        }
    if authority_approved is None:
        authority_approved = os.environ.get(
            "GOVERNANCE_AUTHORITY_APPROVAL", ""
        ).casefold() in {"1", "true", "yes"}

    if tier == 1:
        allowed = True
        message = "tier-1: read-only, direct execution"
    elif tier == 2 and confirmed:
        allowed = True
        message = "tier-2: confirmed"
    elif tier == 2:
        allowed = False
        message = "tier-2: requires explicit confirmation"
    elif authority_approved:
        allowed = True
        message = "tier-3: governance authority approved"
    else:
        allowed = False
        message = "tier-3: requires governance authority approval"
    audit_log(
        tier,
        command,
        actor,
        allowed,
        message,
        repo_snapshot=repo_snapshot,
        phase="decision",
        result="authorized" if allowed else "denied",
    )
    return allowed, message


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry: git-tier-gate <command>

    Exit 0 = allowed, exit 1 = blocked.
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: git-tier-gate <git-command>", file=sys.stderr)
        return 1
    command = " ".join(args)
    actor = os.environ.get("GIT_AUTHOR_NAME", os.environ.get("USER", "unknown"))
    allowed, message = enforce(command, actor)
    print(f"[git-tier-gate] {message}: git {command}", file=sys.stderr)
    return 0 if allowed else 1
