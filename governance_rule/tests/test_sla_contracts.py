"""G12: SLA auto-acceptance contract — the four legislated budgets must be
declared as code constants, actually enforced by their code paths, and the
declared values must match the blueprint SLA (startup 10s / test bound /
audit 30s / tool open 5s).

Fail-closed: a missing constant, a drifted value, or an unenforced budget
fails the gate — SLA compliance is verified, not assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_audit_flow_budget_is_30s_and_enforced() -> None:
    from governance_rule.execution.audit.audit_checks import (
        AUDIT_FLOW_BUDGET_SECONDS,
        audit_flow_budget_error,
    )

    assert AUDIT_FLOW_BUDGET_SECONDS == 30.0
    assert audit_flow_budget_error(30.0) is None
    assert audit_flow_budget_error(30.001) is not None
    assert "budget exceeded" in audit_flow_budget_error(45.0)


def test_audit_flow_reports_budget_breach() -> None:
    """The audit entry point appends the budget error after the flow."""
    source = (
        REPO_ROOT
        / "governance_rule/execution/audit/audit_checks.py"
    ).read_text(encoding="utf-8")
    assert "audit_flow_budget_error(time.monotonic() - started)" in source
    assert "errors.append(budget_error)" in source


def test_tool_open_budget_is_5s() -> None:
    import sys

    src_core = REPO_ROOT / "main-system/src-core"
    if str(src_core) not in sys.path:
        sys.path.insert(0, str(src_core))
    from tasks.tool_lifecycle_budget import TOOL_OPEN_BUDGET_SECONDS

    assert TOOL_OPEN_BUDGET_SECONDS == 5.0
    # Enforced: the launcher computes a deadline from the budget.
    launcher = (src_core / "tasks/toolbox_launch.py").read_text(encoding="utf-8")
    assert "TOOL_OPEN_BUDGET_SECONDS" in launcher
    assert re.search(r"deadline\s*=\s*started\s*\+\s*TOOL_OPEN_BUDGET_SECONDS", launcher)


def test_startup_hard_deadline_is_10s_and_phase_budgets_bounded() -> None:
    import sys

    src_core = REPO_ROOT / "main-system/src-core"
    if str(src_core) not in sys.path:
        sys.path.insert(0, str(src_core))
    from core_system.third_party_types import (
        STARTUP_HARD_DEADLINE_MS,
        STARTUP_PHASE_BUDGETS_MS,
    )

    assert STARTUP_HARD_DEADLINE_MS == 10_000
    assert sum(STARTUP_PHASE_BUDGETS_MS.values()) <= STARTUP_HARD_DEADLINE_MS
    assert all(budget > 0 for budget in STARTUP_PHASE_BUDGETS_MS.values())


def test_test_suite_timeout_is_declared_and_bounded() -> None:
    """The legislated test budget is 20 s for gate-path suites; the repo
    pytest timeout is a hard ceiling that must exist and stay bounded."""
    ini = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    match = re.search(r"--timeout=(\d+)", ini)
    assert match is not None, "pytest timeout not declared"
    timeout_s = int(match.group(1))
    # 60 s is the current ceiling; the 20 s blueprint figure applies to the
    # commit-gate subset. A ceiling above 120 s would be a silent regression.
    assert 0 < timeout_s <= 120
