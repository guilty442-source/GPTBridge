#!/usr/bin/env python3
"""git-auto-commit.py — per-worktree automatic self-commit entry point.

Usage:
  python scripts/git-auto-commit.py --worktree E:/GPTBridge-worktrees/ui --watch
  python scripts/git-auto-commit.py --all --once
  python scripts/git-auto-commit.py --all --watch   (spawns one watcher per worktree)

Commits only, never pushes (A53/E39 / A46 governance). See
`governance_rule/execution/git_tiers/self_commit.py` for the implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers.self_commit import cli_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_main())