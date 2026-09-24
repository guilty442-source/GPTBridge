"""Regression tests for batched self-health test collection (audit speed).

The self-health barrier used to start one ``pytest --collect-only`` per
declared test file (~80 interpreter starts ≈ 35s of the audit).  Collection
is now batched into one run with per-file attribution parsed from pytest's
node-id and ``ERROR`` lines.

Governor runtime-budget amendment (staged 2026-09-17): the whole governance
audit flow must finish inside a hard 60-second monotonic budget, so the
budget constant and its fail-closed verdict are pinned here too.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from governance_rule.execution.audit import audit_checks  # noqa: E402
from governance_rule.execution.audit.audit_self_health import (  # noqa: E402
    _batched_collection_results,
    _verify_self_health_test_files,
)

_VENV_PYTHON = ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"


@pytest.mark.skipif(
    not _VENV_PYTHON.is_file(), reason="project venv python unavailable"
)
def test_batched_collection_attributes_good_empty_and_broken(tmp_path: Path) -> None:
    (tmp_path / "good_test.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "empty_test.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "broken_test.py").write_text("def test_bad(:\n    pass\n", encoding="utf-8")

    results = _batched_collection_results(
        tmp_path,
        str(_VENV_PYTHON),
        ["good_test.py", "empty_test.py", "broken_test.py"],
    )
    assert results is not None
    assert results["good_test.py"][0] is True
    assert results["empty_test.py"] == (False, "no tests collected")
    assert results["broken_test.py"][0] is False


def test_verifier_flags_missing_declared_files(tmp_path: Path) -> None:
    errors: list[str] = []
    _verify_self_health_test_files(tmp_path, errors)
    assert any("self-health test file is missing" in error for error in errors)


def test_governance_audit_flow_budget_is_declared() -> None:
    assert audit_checks.AUDIT_FLOW_BUDGET_SECONDS == 60.0


def test_governance_audit_flow_budget_fails_closed() -> None:
    assert audit_checks.audit_flow_budget_error(1.0) is None
    assert audit_checks.audit_flow_budget_error(60.0) is None
    error = audit_checks.audit_flow_budget_error(60.001)
    assert error is not None
    assert "budget exceeded" in error
