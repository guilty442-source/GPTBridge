#!/usr/bin/env python3
"""git-gate.py -- Git operation tier governance wrapper.

Usage:
  python scripts/git-gate.py <git-command> [args...]

Thin CLI adapter (A375): classification, authorization and execution all
come from ``governance_rule.execution.git_tiers`` -- this file only owns the
interactive confirmation prompt and the usage banner.

Tier 1: read-only, direct execution (no prompt).
Tier 2: general write; an interactive yes executes through
        ``GitRepository.run(confirmed=True)`` which records the decision as a
        LEGACY_CONFIRM entry in the capability ledger.
Tier 3: high-risk; this CLI offers no approval path and fails closed.
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
        print("\nTier 2 (write, interactive confirmation):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER2_OPS))}", file=sys.stderr)
        print("\nTier 3 (high-risk, governance authority required):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER3_OPS))}", file=sys.stderr)
        return 1

    actor = os.environ.get("GIT_AUTHOR_NAME", os.environ.get("USER", "unknown"))
    tier = classify(" ".join(args))

    if tier == 2 and not _prompt_confirmed():
        print("[git-gate] denied: tier-2 not confirmed", file=sys.stderr)
        return 1
    if tier == 3:
        print(
            "[git-gate] denied: tier-3 requires governance authority approval; "
            "no CLI approval path exists",
            file=sys.stderr,
        )
        return 1

    try:
        result = GitRepository(project_root).run(
            args,
            confirmed=True if tier == 2 else None,
            actor=actor,
        )
    except PermissionError as exc:
        print(f"[git-gate] denied: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())