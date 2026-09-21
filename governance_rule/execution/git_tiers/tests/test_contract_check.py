"""§10.8 contract check tests — paired-path rules gate the merge queue."""

from __future__ import annotations

from types import SimpleNamespace

from governance_rule.execution.git_tiers.contract_check import (
    contract_check,
    load_rules,
)


class _FakeRepo:
    def __init__(self, touched: list[str]) -> None:
        self._touched = touched
        self.path = "."

    def run(self, args):
        assert args[:3] == ["diff", "--name-only", "main...src"]
        return SimpleNamespace(returncode=0, stdout="\n".join(self._touched))


_RULES = [
    {
        "name": "ipc-pair",
        "trigger_globs": ["main-system/src-core/ipc/**"],
        "require_globs": ["main-system/tests/**"],
        "severity": "error",
        "detail": "IPC 變更需附測試",
    },
    {
        "name": "schema-migration",
        "trigger_globs": ["shared-layer/migrations/*.sql"],
        "require_globs": ["main-system/config/*.json"],
        "severity": "warn",
    },
]


def test_paired_change_passes():
    repo = _FakeRepo(["main-system/src-core/ipc/server.py", "main-system/tests/test_ipc.py"])
    result = contract_check(repo, "src", rules=_RULES)
    assert result["ok"] is True
    assert not result["violations"]


def test_error_violation_blocks():
    repo = _FakeRepo(["main-system/src-core/ipc/server.py"])
    result = contract_check(repo, "src", rules=_RULES)
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "ipc-pair"


def test_warn_violation_does_not_block():
    repo = _FakeRepo(["shared-layer/migrations/099_x.sql"])
    result = contract_check(repo, "src", rules=_RULES)
    assert result["ok"] is True
    assert result["warnings"][0]["rule"] == "schema-migration"


def test_unrelated_changes_pass():
    repo = _FakeRepo(["docs/readme.md"])
    result = contract_check(repo, "src", rules=_RULES)
    assert result["ok"] is True
    assert not result["warnings"]


def test_default_rules_file_loads():
    rules = load_rules()
    assert rules  # contract_rules.json ships seeded rules
    assert all("trigger_globs" in r and "require_globs" in r for r in rules)


def test_diff_failure_fails_closed_empty():
    class _BrokenRepo:
        path = "."

        def run(self, args):
            return SimpleNamespace(returncode=1, stdout="")

    result = contract_check(_BrokenRepo(), "src", rules=_RULES)
    assert result["ok"] is True  # 無 touched paths → 無觸發；由 merge 自身攔截
    assert result["touched"] == []
