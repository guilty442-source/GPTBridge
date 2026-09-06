#!/usr/bin/env python3
"""git-gate.py — Git operation tier governance wrapper.

Usage:
  python scripts/git-gate.py <git-command> [args...]

Wraps git commands with A53/E39 three-tier enforcement:
  Tier 1: read-only, direct execution (no prompt)
  Tier 2: general write, requires confirmation (y/N prompt or GOVERNANCE_CONFIRM=1)
  Tier 3: high-risk, requires governance authority approval (GOVERNANCE_AUTHORITY_APPROVAL=1)

Audit ledger: governance_rule/execution/audit/git_tier_audit.jsonl (A46)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Windows: suppress console window for background subprocess calls
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers import classify, enforce, audit_log, TIER1_OPS, TIER2_OPS, TIER3_OPS


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: git-gate.py <git-command> [args...]", file=sys.stderr)
        print("\nTier 1 (read-only, direct):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER1_OPS))}", file=sys.stderr)
        print("\nTier 2 (write, requires confirmation):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER2_OPS))}", file=sys.stderr)
        print("\nTier 3 (high-risk, requires governance approval):", file=sys.stderr)
        print(f"  {', '.join(sorted(TIER3_OPS))}", file=sys.stderr)
        return 1

    command = " ".join(args)
    actor = os.environ.get("GIT_AUTHOR_NAME", os.environ.get("USER", "unknown"))
    tier = classify(command)

    # Show tier classification
    print(f"[git-gate] classified as Tier-{tier}: git {command}", file=sys.stderr)

    allowed, message = enforce(command, actor)
    if not allowed:
        print(f"[git-gate] BLOCKED: {message}", file=sys.stderr)
        if tier == 2:
            print("[git-gate] To confirm: set GOVERNANCE_CONFIRM=1 or re-run with confirmation", file=sys.stderr)
        elif tier == 3:
            print("[git-gate] To approve: set GOVERNANCE_AUTHORITY_APPROVAL=1 (governance authority only)", file=sys.stderr)
        return 1

    # Tier 2 interactive confirmation if not pre-confirmed
    if tier == 2 and os.environ.get("GOVERNANCE_CONFIRM", "").lower() not in ("1", "true", "yes"):
        response = input(f"[git-gate] Tier-2 operation. Proceed? (y/N): ")
        if response.lower() not in ("y", "yes"):
            audit_log(tier, command, actor, approved=False, detail="tier-2 user-declined")
            print("[git-gate] Cancelled by user.", file=sys.stderr)
            return 1
        audit_log(tier, command, actor, approved=True, detail="tier-2 user-confirmed")

    # Execute the actual git command
    git_args = ["git"] + args
    result = subprocess.run(git_args, cwd=str(project_root), creationflags=_CREATE_NO_WINDOW)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
