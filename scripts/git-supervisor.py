#!/usr/bin/env python3
"""Supervised persistent automation for the parallel worktree pipeline.

Usage:
  python scripts/git-supervisor.py --root E:/GPTBridge --start
  python scripts/git-supervisor.py --root E:/GPTBridge --status
  python scripts/git-supervisor.py --root E:/GPTBridge --stop
  python scripts/git-supervisor.py --root E:/GPTBridge --install-task
  python scripts/git-supervisor.py --root E:/GPTBridge --uninstall-task

Spawns one self-commit watcher per worktree and periodically integrates
worker branches into main. See
`governance_rule/execution/git_tiers/automation_supervisor.py` for the
implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from governance_rule.execution.git_tiers.automation_supervisor import cli_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_main())