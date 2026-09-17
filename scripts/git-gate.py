#!/usr/bin/env python3
"""git-gate.py — Git operation tier governance wrapper.

Usage:
  python scripts/git-gate.py <git-command> [args...]

Thin CLI adapter (A375): classification, authorization and execution all
come from ``governance_rule.execution.git_tiers`` — this file only owns the
interactive confirmation prompt and the usage banner.

Tier 1: read-only, direct execution (no prompt).
Tier 2: general write; an interactive yes issues a one-time
        USER_CONFIRMATION capability bound to this exact command and executes
        it through the capability gate.
Tier 3: high-risk; no implicit, environment or automation approval exists.
        A governance authority capability must be issued externally and
        presented through the Python API; without it the command fails
        closed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers import (
    TIER1_OPS,
    TIER2_OPS,
    TIER3_OPS,
    classify,
)
from governance_rule.execution.git_tiers.capability import (
    IssuerClass,
    repository_id_for,
)
from governance_rule.execution.git_tiers.capability_gate import (
    execute_with_capability,
)
from governance_rule.execution.git_tiers.capability_issue import issue_capability
from governance_rule.execution.git_tiers.git_repository import GitRepository


def _prompt_confirmed() -> bool:
    try:
        response = input("[git-gate] Tier-2 operation. Proceed? (y/N): ")
    except (EOFError, KeyboardInterrupt):
        return False
    return response.strip().casefold() in ("y", "yes")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: git-gate.py <git-command> [args...]", file=sys.stderr)
        print("\nTier 1 (read-only, direct):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER1_OPS))}", file=sys.stderr)
        print("\nTier 2 (write, one-time confirmation capability):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER2_OPS))}", file=sys.stderr)
        print("\nTier 3 (high-risk, external authority capability):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER3_OPS))}", file=sys.stderr)
        return 1

    command = " ".join(args)
    actor = os.environ.get(
        "GIT_AUTHOR_NAME",
        os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
    )
    tier = classify(command)
    repository = GitRepository(project_root)
    repository_id = repository_id_for(project_root)

    print(f"[git-gate] classified as Tier-{tier}: git {command}", file=sys.stderr)

    capability = None
    if tier == 2:
        if not _prompt_confirmed():
            denied = execute_with_capability(
                args, None, tier=2, actor=actor,
                repository_id=repository_id, repo_path=project_root,
            )
            print(f"[git-gate] Cancelled: {denied.code}", file=sys.stderr)
            return 1
        capability = issue_capability(
            issuer_class=IssuerClass.USER_CONFIRMATION,
            issuer_id=f"console/{actor}",
            actor=actor,
            repository_id=repository_id,
            command=args,
            tier=2,
        )

    if tier == 3:
        denied = execute_with_capability(
            args, None, tier=3, actor=actor,
            repository_id=repository_id, repo_path=project_root,
        )
        print(
            f"[git-gate] BLOCKED: {denied.code} — tier-3 requires an external "
            "governance authority capability; environment approval is closed",
            file=sys.stderr,
        )
        return 1

    result = execute_with_capability(
        args, capability, tier=tier, actor=actor,
        repository_id=repository_id, repo_path=project_root,
    )
    if result.allowed is False:
        print(
            f"[git-gate] BLOCKED: {result.code}: {result.detail}",
            file=sys.stderr,
        )
        return 1
    completed = result.execution_result
    return int(getattr(completed, "returncode", 1))


if __name__ == "__main__":
    sys.exit(main())
