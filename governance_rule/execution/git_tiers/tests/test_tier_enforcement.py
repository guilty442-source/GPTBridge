"""consolidated test suite (A57/E43)

maintenance sovereign for self-health (self-test collection).

Tests for classify(), enforce(), and tier boundary edge cases (A53/E39).
These tests verify the three-tier git operation governance without requiring
a live git repository — they exercise the pure classification and authorization
logic that is the core enforcement surface.
"""
import os

import pytest

from governance_rule.execution.git_tiers import (
    TIER1_OPS,
    TIER2_OPS,
    TIER3_OPS,
    audit_log,
    classify,
    enforce,
)


# ---------------------------------------------------------------------------
# classify() — tier classification
# ---------------------------------------------------------------------------

class TestClassifyTier1:
    """Tier-1: read-only, high-frequency, direct execution."""

    @pytest.mark.parametrize("cmd", [
        "git --version",
        "git status",
        "git log",
        "git diff",
        "git show",
        "git branch",
        "git remote",
        "git blame HEAD",
        "git ls-files",
        "git cat-file -p HEAD",
        "git rev-parse HEAD",
        "git describe",
        "git tag -l",
        "git for-each-ref",
        "git stash list",
        "git config --get user.name",
        "git config --list",
    ])
    def test_canonical_tier1_ops(self, cmd: str) -> None:
        assert classify(cmd) == 1

    @pytest.mark.parametrize("cmd", [
        "worktree list",
        "worktree list --porcelain",
        "worktree status",
        "worktree prune --dry-run",
        "reflog show",
        "fsck",
        "count-objects",
        "shortlog",
        "annotate",
        "name-rev",
        "rev-list",
        "ls-tree",
        "ls-remote",
        "merge-base --is-ancestor",
    ])
    def test_extended_tier1_ops(self, cmd: str) -> None:
        """TIER-1 extensions beyond A53 explicit list — all read-only."""
        assert classify(cmd) == 1

    def test_tier1_without_git_prefix(self) -> None:
        assert classify("status") == 1
        assert classify("log --oneline") == 1

    def test_tier1_case_insensitive(self) -> None:
        assert classify("git STATUS") == 1
        assert classify("Git Log") == 1


class TestClassifyTier2:
    """Tier-2: general write, requires confirmation."""

    @pytest.mark.parametrize("cmd", [
        "git add",
        "git commit -m test",
        "git stash",
        "git stash push",
        "git stash pop",
        "git stash apply",
        "git checkout main",
        "git switch develop",
        "git merge feature",
        "git fetch origin",
        "git push origin main",
        "git rebase main",
        "git cherry-pick abc123",
        "git revert abc123",
        "git worktree add",
        "git worktree remove",
    ])
    def test_canonical_tier2_ops(self, cmd: str) -> None:
        assert classify(cmd) == 2

    @pytest.mark.parametrize("cmd", [
        "worktree lock",
        "worktree unlock",
        "worktree move",
        "worktree prune",
        "mv",
        "restore",
        "switch -c",
        "pull",
        "clone",
    ])
    def test_extended_tier2_ops(self, cmd: str) -> None:
        """TIER-2 extensions — pull/clone involve network (A58 governance)."""
        assert classify(cmd) == 2


class TestClassifyTier3:
    """Tier-3: high-risk, strictly restricted, requires governance authority."""

    @pytest.mark.parametrize("cmd", [
        "git push --force",
        "git push --force-with-lease",
        "git push -f",
        "git push +main",
        "git commit --amend",
        "git reset --hard",
        "git reset --soft",
        "git branch -D feature",
        "git filter-branch",
        "git filter-repo",
        "git rebase -i",
        "git rebase --interactive",
        "git rebase --root",
        "git gc --prune",
        "git reflog expire",
        "git update-ref -d",
        "git clean -fd",
        "git clean -fdx",
        "git stash drop",
        "git stash clear",
        "git push --delete origin",
        "git tag -d v1",
        "git replace",
        "git notes remove",
    ])
    def test_tier3_ops(self, cmd: str) -> None:
        assert classify(cmd) == 3

    def test_commit_amend_is_tier3_conservative(self) -> None:
        """A53 specifies 'amend-pushed' as TIER-3; we classify ALL amend as
        TIER-3 per A11 fail-closed — safer to require approval always."""
        assert classify("git commit --amend -m new") == 3

    def test_branch_d_is_tier3_conservative(self) -> None:
        """A53 lists only branch -D; branch -d is also TIER-3 here per A11."""
        assert classify("git branch -d feature") == 3


class TestClassifyUnknown:
    """Unknown operations fail closed as Tier-3 (A11)."""

    def test_unknown_command_is_tier3(self) -> None:
        assert classify("git some-unknown-command") == 3

    def test_empty_command_is_tier3(self) -> None:
        assert classify("") == 3

    def test_whitespace_command_is_tier3(self) -> None:
        assert classify("   ") == 3

    def test_garbage_command_is_tier3(self) -> None:
        assert classify("git $$$invalid$$$") == 3


class TestClassifyTier3Precedence:
    """Tier-3 is checked first — a command matching both TIER-3 and TIER-2
    patterns must be classified as TIER-3 (most restrictive wins)."""

    def test_force_push_overrides_push(self) -> None:
        """push --force contains 'push' (TIER-2) but must be TIER-3."""
        assert classify("git push --force origin main") == 3

    def test_reset_hard_overrides_nothing(self) -> None:
        assert classify("git reset --hard HEAD~1") == 3


# ---------------------------------------------------------------------------
# enforce() — authorization logic
# ---------------------------------------------------------------------------

class TestEnforceTier1:
    """Tier-1 operations are always allowed (read-only, direct execution)."""

    def test_tier1_allowed_without_confirmation(self) -> None:
        allowed, message = enforce("git status", actor="test")
        assert allowed is True
        assert "tier-1" in message

    def test_tier1_allowed_without_authority(self) -> None:
        allowed, _ = enforce("git log --oneline", actor="test")
        assert allowed is True


class TestEnforceTier2:
    """Tier-2 operations require explicit confirmation."""

    def test_tier2_denied_without_confirmation(self) -> None:
        allowed, message = enforce("git commit -m test", actor="test")
        assert allowed is False
        assert "tier-2" in message
        assert "confirmation" in message

    def test_tier2_allowed_with_confirmation(self) -> None:
        allowed, message = enforce(
            "git commit -m test", actor="test", confirmed=True
        )
        assert allowed is True
        assert "tier-2" in message
        assert "confirmed" in message

    def test_tier2_denied_with_authority_only(self) -> None:
        """Authority approval alone does not satisfy TIER-2 — confirmation
        is the required gate for TIER-2."""
        allowed, _ = enforce(
            "git commit -m test", actor="test", authority_approved=True
        )
        assert allowed is False


class TestEnforceTier3:
    """Tier-3 operations require governance authority approval."""

    def test_tier3_denied_without_authority(self) -> None:
        allowed, message = enforce("git push --force", actor="test")
        assert allowed is False
        assert "tier-3" in message
        assert "authority" in message

    def test_tier3_denied_with_confirmation_only(self) -> None:
        """Confirmation alone does not satisfy TIER-3 — authority approval
        is the required gate for TIER-3."""
        allowed, _ = enforce(
            "git push --force", actor="test", confirmed=True
        )
        assert allowed is False

    def test_tier3_allowed_with_authority(self) -> None:
        allowed, message = enforce(
            "git push --force", actor="test", authority_approved=True
        )
        assert allowed is True
        assert "tier-3" in message
        assert "approved" in message


class TestEnforceEnvVars:
    """enforce() reads GOVERNANCE_CONFIRM and GOVERNANCE_AUTHORITY_APPROVAL
    from environment when explicit parameters are not provided."""

    def test_tier2_via_env_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOVERNANCE_CONFIRM", "1")
        allowed, _ = enforce("git commit -m test", actor="test")
        assert allowed is True

    def test_tier3_via_env_authority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOVERNANCE_AUTHORITY_APPROVAL", "true")
        allowed, _ = enforce("git reset --hard", actor="test")
        assert allowed is True

    def test_tier3_denied_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
        monkeypatch.delenv("GOVERNANCE_CONFIRM", raising=False)
        allowed, _ = enforce("git reset --hard", actor="test")
        assert allowed is False


# ---------------------------------------------------------------------------
# audit_log() — ledger entry creation (A46)
# ---------------------------------------------------------------------------

class TestAuditLog:
    """audit_log writes a JSONL entry to the audit ledger (A46)."""

    def test_audit_log_returns_entry_dict(self) -> None:
        entry = audit_log(
            tier=1,
            command="git status",
            actor="test-audit",
            approved=True,
            detail="test entry",
            repo_snapshot={"head_revision": "abc", "branch": "main",
                           "dirty_files": [], "staged_files": [],
                           "untracked_files": []},
        )
        assert isinstance(entry, dict)
        assert entry["tier"] == 1
        assert entry["command"] == "git status"
        assert entry["actor"] == "test-audit"
        assert entry["approved"] is True
        assert entry["head_revision"] == "abc"
        assert entry["branch"] == "main"

    def test_audit_log_operation_inferred_from_command(self) -> None:
        entry = audit_log(
            tier=2,
            command="git commit -m test",
            actor="test",
            approved=True,
            repo_snapshot={"head_revision": "", "branch": "main",
                           "dirty_files": [], "staged_files": [],
                           "untracked_files": []},
        )
        assert entry["operation"] == "git"


# ---------------------------------------------------------------------------
# Tier set integrity — no overlap between tiers (A53 separation)
# ---------------------------------------------------------------------------

class TestTierSetIntegrity:
    """Verify TIER1/TIER2/TIER3 sets are disjoint — no operation appears in
    multiple tiers, which would create classification ambiguity."""

    def test_no_tier1_tier2_overlap(self) -> None:
        overlap = TIER1_OPS & TIER2_OPS
        assert not overlap, f"TIER1/TIER2 overlap: {overlap}"

    def test_no_tier1_tier3_overlap(self) -> None:
        overlap = TIER1_OPS & TIER3_OPS
        assert not overlap, f"TIER1/TIER3 overlap: {overlap}"

    def test_no_tier2_tier3_overlap(self) -> None:
        overlap = TIER2_OPS & TIER3_OPS
        assert not overlap, f"TIER2/TIER3 overlap: {overlap}"

    def test_tier1_contains_canonical_a53_ops(self) -> None:
        """A53 TIER-1-OPS canonical subset must be present."""
        required = {"status", "log", "diff", "show", "branch", "remote",
                    "blame", "ls-files", "cat-file", "rev-parse", "describe",
                    "tag -l", "for-each-ref", "stash list", "config --get"}
        missing = required - TIER1_OPS
        assert not missing, f"Missing canonical TIER-1 ops: {missing}"

    def test_tier2_contains_canonical_a53_ops(self) -> None:
        """A53 TIER-2-OPS canonical subset must be present."""
        required = {"add", "commit", "stash", "checkout", "switch", "merge",
                    "fetch", "push", "rebase", "cherry-pick", "revert"}
        missing = required - TIER2_OPS
        assert not missing, f"Missing canonical TIER-2 ops: {missing}"

    def test_tier3_contains_canonical_a53_ops(self) -> None:
        """A53 TIER-3-OPS canonical subset must be present."""
        required = {"push --force", "push --force-with-lease",
                    "commit --amend", "reset --hard", "reset --soft",
                    "branch -D", "filter-branch", "filter-repo",
                    "rebase -i", "rebase --root", "gc --prune",
                    "reflog expire", "update-ref -d", "clean -fd",
                    "stash drop", "stash clear"}
        missing = required - TIER3_OPS
        assert not missing, f"Missing canonical TIER-3 ops: {missing}"
