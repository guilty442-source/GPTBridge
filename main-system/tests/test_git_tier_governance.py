from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution.git_tiers import (
    TIER1_OPS,
    TIER2_OPS,
    TIER3_OPS,
    classify,
    enforce,
    audit_log,
)


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,expected_tier",
    [
        ("status", 1),
        ("log", 1),
        ("diff", 1),
        ("show HEAD", 1),
        ("branch", 1),
        ("remote -v", 1),
        ("blame src/main.py", 1),
        ("ls-files", 1),
        ("worktree list", 1),
        ("config --get user.name", 1),
        ("rev-parse HEAD", 1),
        ("describe --tags", 1),
        ("for-each-ref", 1),
        ("stash list", 1),
        ("fsck", 1),
        ("add .", 2),
        ("commit -m test", 2),
        ("stash push", 2),
        ("checkout main", 2),
        ("switch -c feature", 2),
        ("merge develop", 2),
        ("fetch origin", 2),
        ("push origin main", 2),
        ("rebase main", 2),
        ("cherry-pick abc123", 2),
        ("revert abc123", 2),
        ("worktree add ../wt", 2),
        ("pull origin main", 2),
        ("clone https://example.com/repo.git", 2),
        ("push --force", 3),
        ("push --force-with-lease", 3),
        ("push -f", 3),
        ("commit --amend", 3),
        ("reset --hard HEAD~1", 3),
        ("reset --soft HEAD~1", 3),
        ("branch -D feature", 3),
        ("branch -d feature", 3),
        ("filter-branch -- --all", 3),
        ("filter-repo --force", 3),
        ("rebase -i HEAD~3", 3),
        ("rebase --interactive", 3),
        ("rebase --root", 3),
        ("gc --prune", 3),
        ("gc --aggressive", 3),
        ("reflog expire --expire=now --all", 3),
        ("update-ref -d refs/heads/old", 3),
        ("clean -fd", 3),
        ("clean -fdx", 3),
        ("stash drop", 3),
        ("stash clear", 3),
        ("push --delete origin old", 3),
        ("tag -d v1.0", 3),
    ],
)
def test_classify_returns_expected_tier(command: str, expected_tier: int) -> None:
    assert classify(command) == expected_tier


def test_classify_with_leading_git_prefix() -> None:
    assert classify("git status") == 1
    assert classify("git commit -m test") == 2
    assert classify("git push --force") == 3


def test_classify_unknown_defaults_to_tier_2() -> None:
    assert classify("some-unknown-command") == 2


def test_tier_operation_sets_are_disjoint() -> None:
    assert TIER1_OPS & TIER2_OPS == set()
    assert TIER1_OPS & TIER3_OPS == set()
    assert TIER2_OPS & TIER3_OPS == set()


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def test_tier1_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOVERNANCE_CONFIRM", raising=False)
    monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
    allowed, message = enforce("status", actor="test")
    assert allowed is True
    assert "tier-1" in message


def test_tier2_blocked_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOVERNANCE_CONFIRM", raising=False)
    monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
    allowed, message = enforce("commit -m test", actor="test")
    assert allowed is False
    assert "tier-2" in message


def test_tier2_allowed_with_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOVERNANCE_CONFIRM", "1")
    monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
    allowed, message = enforce("commit -m test", actor="test")
    assert allowed is True
    assert "tier-2" in message


def test_tier3_blocked_without_authority_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOVERNANCE_CONFIRM", raising=False)
    monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
    allowed, message = enforce("push --force", actor="test")
    assert allowed is False
    assert "tier-3" in message
    assert "governance authority" in message


def test_tier3_blocked_with_only_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOVERNANCE_CONFIRM", "1")
    monkeypatch.delenv("GOVERNANCE_AUTHORITY_APPROVAL", raising=False)
    allowed, message = enforce("push --force", actor="test")
    assert allowed is False
    assert "tier-3" in message


def test_tier3_allowed_with_authority_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOVERNANCE_CONFIRM", raising=False)
    monkeypatch.setenv("GOVERNANCE_AUTHORITY_APPROVAL", "1")
    allowed, message = enforce("push --force", actor="test")
    assert allowed is True
    assert "tier-3" in message


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------

def test_audit_log_writes_jsonl_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "audit.jsonl"
    monkeypatch.setattr(
        "governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH",
        ledger,
    )
    audit_log(2, "commit -m test", "test-actor", approved=True, detail="test-detail")
    content = ledger.read_text(encoding="utf-8").strip()
    entry = json.loads(content)
    assert entry["tier"] == 2
    assert entry["command"] == "commit -m test"
    assert entry["actor"] == "test-actor"
    assert entry["approved"] is True
    assert entry["detail"] == "test-detail"
    assert "timestamp" in entry


# ---------------------------------------------------------------------------
# Enforcement infrastructure presence (A53/E39)
# ---------------------------------------------------------------------------

def test_git_tiers_module_is_protected_governance_source() -> None:
    from governance_rule.governance_policy import governance_policy_snapshot
    policy = governance_policy_snapshot()
    assert "governance_rule/execution/git_tiers/__init__.py" in policy.authority_files


def test_git_tiers_module_is_required_enforcement_source() -> None:
    from governance_rule.execution.audit import REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES
    assert "governance_rule/execution/git_tiers/__init__.py" in REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES


def test_git_gate_wrapper_exists() -> None:
    gate = ROOT / "scripts" / "git-gate.py"
    assert gate.is_file(), "scripts/git-gate.py must exist"
    text = gate.read_text(encoding="utf-8")
    assert "from governance_rule.execution.git_tiers import" in text


def test_pre_push_hook_exists_and_enforces_force_push_blocking() -> None:
    hook = ROOT / ".git" / "hooks" / "pre-push"
    assert hook.is_file(), ".git/hooks/pre-push must exist"
    text = hook.read_text(encoding="utf-8")
    assert "GOVERNANCE_AUTHORITY_APPROVAL" in text
    assert "force" in text.lower()


def test_audit_ledger_exists() -> None:
    ledger = ROOT / "governance_rule" / "execution" / "audit" / "git_tier_audit.jsonl"
    assert ledger.is_file(), "git tier audit ledger must exist"


def test_governance_audit_passes_with_git_tier_checks() -> None:
    from governance_rule.execution.audit import audit_runtime_governance
    errors = audit_runtime_governance()
    git_errors = [e for e in errors if "git tier" in e.lower() or "git gate" in e.lower() or "pre-push" in e.lower()]
    assert git_errors == [], f"git tier enforcement errors: {git_errors}"
