"""Sanctioned adapter: main-system git-automation task -> Git tier.

The Git tier dependency direction is frozen (A529/A495): ``main-system``
modules must not import ``governance_rule.execution.git_tiers``
internals directly.  This facade is the single governed entry point for
the in-process ``GitAutomationService`` — it owns the tier coupling and
re-exports only the operations the task needs, the same way
``execution/audit`` already bridges into the tier.
"""

from __future__ import annotations

from governance_rule.execution.git_tiers.generation_snapshot import (
    generation_snapshot,
    notify_changed,
)
from governance_rule.execution.git_tiers.git_repository import GitRepository
from governance_rule.execution.git_tiers.process_lock import LockBusyError
from governance_rule.execution.git_tiers.self_commit import run_once
from governance_rule.execution.git_tiers.worktree_manager import WorktreeManager
from governance_rule.execution.git_tiers.workspace_sync import synchronize

__all__ = [
    "GitRepository",
    "LockBusyError",
    "WorktreeManager",
    "generation_snapshot",
    "notify_changed",
    "run_once",
    "synchronize",
]
