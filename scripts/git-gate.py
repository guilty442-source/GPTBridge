#!/usr/bin/env python3
"""git-gate.py ??Git operation tier governance wrapper.

Usage:
  python scripts/git-gate.py <git-command> [args...]

Wraps git commands with A53/E39 three-tier enforcement:
  Tier 1: read-only, direct execution (no prompt)
  Tier 2: general write, requires confirmation (y/N prompt or GOVERNANCE_CONFIRM=1)
  Tier 3: high-risk, requires governance authority approval (GOVERNANCE_AUTHORITY_APPROVAL=1)

Audit ledger: governance_rule/execution/audit/git_tier_audit.jsonl (A46)

Flow:
  Git Command → git-gate.py → git_tiers.classify()
    ├─ Tier 1 → 直接執行
    ├─ Tier 2 → GOVERNANCE_CONFIRM (env or interactive prompt)
    └─ Tier 3 → GOVERNANCE_AUTHORITY_APPROVAL
  → Git → Audit Ledger (single entry per operation)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers import (
    classify,
    audit_log,
    TIER1_OPS,
    TIER2_OPS,
    TIER3_OPS,
)
from governance_rule.execution.git_tiers.git_repository import GitRepository


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
    actor = os.environ.get(
        "GIT_AUTHOR_NAME",
        os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
    )
    tier = classify(command)
    repository = GitRepository(project_root)

    # Show tier classification
    print(f"[git-gate] classified as Tier-{tier}: git {command}", file=sys.stderr)

    # ── Tier 1: read-only → direct execution ──────────────────────
    if tier == 1:
        return repository.run(args, actor=actor).returncode

    # ── Tier 2: general write → GOVERNANCE_CONFIRM or interactive ─
    if tier == 2:
        confirmed = os.environ.get("GOVERNANCE_CONFIRM", "").lower() in (
            "1", "true", "yes",
        )
        if not confirmed:
            # Interactive confirmation prompt
            try:
                response = input(
                    f"[git-gate] Tier-2 operation. Proceed? (y/N): "
                )
            except (EOFError, KeyboardInterrupt):
                audit_log(
                    tier, command, actor, approved=False,
                    detail="tier-2 user-declined (no input)",
                )
                print("[git-gate] Cancelled.", file=sys.stderr)
                return 1
            if response.lower() not in ("y", "yes"):
                audit_log(
                    tier, command, actor, approved=False,
                    detail="tier-2 user-declined",
                )
                print("[git-gate] Cancelled by user.", file=sys.stderr)
                return 1
        return repository.run(
            args,
            confirmed=True,
            authority_approved=False,
            actor=actor,
        ).returncode

    # ── Tier 3: high-risk → GOVERNANCE_AUTHORITY_APPROVAL ─────────
    approved = os.environ.get("GOVERNANCE_AUTHORITY_APPROVAL", "").lower() in (
        "1", "true", "yes",
    )
    if not approved:
        audit_log(
            tier, command, actor, approved=False,
            detail="tier-3 requires governance authority approval",
        )
        print(
            "[git-gate] BLOCKED: tier-3 requires governance authority approval "
            "(set GOVERNANCE_AUTHORITY_APPROVAL=1)",
            file=sys.stderr,
        )
        return 1
    return repository.run(
        args,
        confirmed=True,
        authority_approved=True,
        actor=actor,
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
