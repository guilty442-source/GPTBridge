"""consolidated test suite (A57/E43)

maintenance sovereign for self-health (self-test collection).
"""
import pytest

from governance_rule.execution.git_tiers import classify
from governance_rule.execution.git_tiers.git_repository import GitRepository
from governance_rule.execution.git_tiers.worktree_manager import WorktreeManager


def test_worktree_status_is_tier_1() -> None:
    assert classify("worktree status") == 1


def test_worktree_prune_dry_run_is_tier_1() -> None:
    assert classify("worktree prune --dry-run") == 1


def test_worktree_create_is_tier_2() -> None:
    assert classify("worktree create") == 2


def test_worktree_lock_unlock_prune_are_tier_2() -> None:
    assert classify("worktree lock") == 2
    assert classify("worktree unlock") == 2
    assert classify("worktree prune") == 2


def test_worktree_manager_instantiation() -> None:
    repo = GitRepository()
    manager = WorktreeManager(repo)
    assert isinstance(manager.list_worktrees(), list)


def test_branch_occupancy_returns_dict() -> None:
    repo = GitRepository()
    manager = WorktreeManager(repo)
    assert isinstance(manager.branch_occupancy(), dict)


def test_stale_worktrees_returns_list() -> None:
    repo = GitRepository()
    manager = WorktreeManager(repo)
    assert isinstance(manager.stale_worktrees(), list)
