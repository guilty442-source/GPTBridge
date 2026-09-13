#!/usr/bin/env python3
"""Synchronize all GPTBridge worktrees safely (commit, integrate, fast-forward).

Usage:
  python scripts/git-worktree-sync.py --root E:/GPTBridge
  python scripts/git-worktree-sync.py --root E:/GPTBridge --watch --interval 60 --no-commit --push

Commit each checkout, merge worker branches into main, audit the integrated
result, then fast-forward all clean worktrees. See
`governance_rule/execution/git_tiers/workspace_sync.py` for the
implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers.workspace_sync import cli_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_main())
