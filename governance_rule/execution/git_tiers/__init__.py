"""Git operation tier governance — implements A53/E39.

Three-tier classification:
  Tier 1: read-only, high-frequency, direct execution
  Tier 2: general write, requires confirmation
  Tier 3: high-risk, strictly restricted, requires governance-authority approval

Enforcement: hook + governance gate + audit ledger (A46).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Final

PROJECT_ROOT = Path(__file__).resolve().parents[3]
AUDIT_LEDGER_PATH = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "git_tier_audit.jsonl"

TIER1_OPS: Final[frozenset[str]] = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "blame",
    "ls-files", "cat-file", "rev-parse", "describe", "tag -l",
    "for-each-ref", "stash list", "config --get", "config --list",
    "worktree list", "worktree list --porcelain", "worktree status",
    "worktree prune --dry-run", "reflog show", "fsck", "count-objects", "shortlog",
    "annotate", "name-rev", "rev-list", "ls-tree", "ls-remote",
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
    "branch -d", "filter-branch", "filter-repo", "rebase -i",
    "rebase --interactive", "rebase --root", "gc --prune", "gc --prune=now",
    "gc --aggressive", "reflog expire", "reflog expire --expire=now",
    "update-ref -d", "clean -fd", "clean -fdx", "stash drop",
    "stash clear", "push --delete", "tag -d", "replace", "notes remove",
})


def classify(command: str) -> int:
    """Classify a git command string into tier 1, 2, or 3.

    Returns 1, 2, or 3. Defaults to 2 for unknown write-ish commands.
    """
    cmd = command.strip().lower()

    # Remove leading "git " if present
    if cmd.startswith("git "):
        cmd = cmd[4:]

    # Check Tier 3 first (most restrictive)
    for op in TIER3_OPS:
        if op in cmd:
            return 3

    # Check Tier 1 (read-only)
    for op in TIER1_OPS:
        if cmd.startswith(op) or cmd == op:
            return 1

    # Check Tier 2 (general write)
    for op in TIER2_OPS:
        if cmd.startswith(op) or cmd == op:
            return 2

    # Unknown — default to Tier 2 (cautious but not blocking)
    return 2


def audit_log(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    *,
    operation: str = "",
    repo_snapshot: dict[str, object] | None = None,
) -> None:
    """Write an audit ledger entry (A46 compliance).

    Captures pre-operation repo state so failed Tier-2/3 operations can
    be recovered to the recorded HEAD revision:

      Before
      HEAD = abc123
           |
      Tier-2 merge
           |
      failure
           |
      Recovery metadata -> abc123

    Fields:
      - timestamp: ISO-8601 local time
      - tier: 1/2/3
      - operation: normalized operation label (e.g. "merge", "push", "commit")
      - command: raw git command string
      - actor: who invoked the operation
      - approved: whether governance approved it
      - detail: human-readable detail / failure reason
      - head_revision: pre-operation HEAD SHA (recovery target)
      - branch: pre-operation branch name
      - dirty_files: unstaged-modified paths before operation
      - staged_files: staged paths before operation
    """
    from .snapshot import _capture_repo_snapshot

    AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = repo_snapshot if repo_snapshot is not None else _capture_repo_snapshot()
    if not operation:
        operation = command.strip().split()[0] if command.strip() else "unknown"
    entry: dict[str, object] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "tier": tier,
        "operation": operation,
        "command": command,
        "actor": actor,
        "approved": approved,
        "detail": detail,
        "head_revision": snapshot["head_revision"],
        "branch": snapshot["branch"],
        "dirty_files": snapshot["dirty_files"],
        "staged_files": snapshot["staged_files"],
    }
    with open(AUDIT_LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def enforce(command: str, actor: str = "unknown") -> tuple[bool, str]:
    """Enforce tier rules on a git command.

    Returns (allowed, message).
    Tier 1: always allowed.
    Tier 2: allowed if GOVERNANCE_CONFIRM env var is set.
    Tier 3: allowed only if GOVERNANCE_AUTHORITY_APPROVAL env var is set.
    """
    tier = classify(command)

    if tier == 1:
        audit_log(tier, command, actor, approved=True, detail="tier-1 direct-exec")
        return True, "tier-1: read-only, direct execution"

    if tier == 2:
        confirmed = os.environ.get("GOVERNANCE_CONFIRM", "").lower() in ("1", "true", "yes")
        if confirmed:
            audit_log(tier, command, actor, approved=True, detail="tier-2 confirmed")
            return True, "tier-2: confirmed"
        audit_log(tier, command, actor, approved=False, detail="tier-2 requires confirmation")
        return False, "tier-2: requires confirmation (set GOVERNANCE_CONFIRM=1)"

    # Tier 3
    approved = os.environ.get("GOVERNANCE_AUTHORITY_APPROVAL", "").lower() in ("1", "true", "yes")
    if approved:
        audit_log(tier, command, actor, approved=True, detail="tier-3 governance-authority-approved")
        return True, "tier-3: governance authority approved"
    audit_log(tier, command, actor, approved=False, detail="tier-3 requires governance authority approval")
    return False, "tier-3: requires governance authority approval (set GOVERNANCE_AUTHORITY_APPROVAL=1)"


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
