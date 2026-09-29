#!/usr/bin/env python3
"""git-gate.py -- Git operation tier governance wrapper.

Usage:
  python scripts/git-gate.py <git-command> [args...]

Thin CLI adapter (A375): classification, authorization and audit recording
come from ``governance_rule.execution.git_tiers`` -- this file only owns the
interactive confirmation prompt, the subprocess exec, and the usage banner.
The resident scheduler/self-commit path is the C# host
(GPTBridge.GitAutomation.exe); this wrapper is for manual invocations.

Tier 1: read-only, direct execution (no prompt).
Tier 2: general write; an interactive yes executes the command and the
        decision is recorded through ``enforce`` in the audit ledger.
Tier 3: high-risk; this CLI offers no approval path and fails closed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers import (
    TIER1_OPS,
    TIER2_OPS,
    TIER3_OPS,
    audit_log,
    classify,
    enforce,
)


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
    command = " ".join(args)
    tier = classify(command)

    confirmed = tier == 2 and _prompt_confirmed()
    allowed, message = enforce(command, actor, confirmed=confirmed or None)
    if not allowed:
        print(f"[git-gate] denied: {message}", file=sys.stderr)
        return 1

    try:
        proc = subprocess.run(
            ["git", *args], cwd=project_root, capture_output=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        audit_log(tier, command, actor, False, str(exc), phase="result",
                  result="failed", returncode=-1)
        print(f"[git-gate] exec failed: {exc}", file=sys.stderr)
        return 1
    audit_log(
        tier, command, actor, True, "executed via git-gate CLI",
        phase="result",
        result="ok" if proc.returncode == 0 else "failed",
        returncode=proc.returncode,
    )
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())