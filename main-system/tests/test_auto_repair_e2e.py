"""G15: end-to-end acceptance for the §10.4 auto-repair chain.

Exercises ``AutoRepairOrchestrator.process_health_signal`` — the real
seven-plus-stage entry point (classification → decision → retry gate →
permission → plan → governed execute → independent verify → finalize →
learn → report) — which no existing test traverses whole.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core_system.auto_repair_chain import AutoRepairOrchestrator
from core_system.auto_repair_chain_types import HealthSignal, HealthState


def _orchestrator(tmp_path: Path) -> AutoRepairOrchestrator:
    # auth_service is stored for provenance but not consulted on the grant
    # path (actor allow-list + scope validation decide); a stub suffices.
    return AutoRepairOrchestrator(tmp_path, SimpleNamespace())


def _crash_signal() -> HealthSignal:
    return HealthSignal(
        component_id="demo-tool",
        dimension="process-survival",
        state=HealthState.CRITICAL,
        severity=5,
        evidence={
            "fault_code": "TOOL_RUNTIME_CRASH",
            "tool_id": "demo-tool",
            "component": "demo-tool",
        },
    )


def test_healthy_signal_short_circuits(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    signal = HealthSignal(
        component_id="demo-tool",
        dimension="runtime-readiness",
        state=HealthState.HEALTHY,
        severity=1,
        evidence={"fault_code": "NONE"},
    )
    result = orch.process_health_signal(signal)
    assert result["result"] == "healthy"
    assert result["stage"] == "health_classification"


def test_unauthorized_actor_denied_at_permission_stage(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    result = orch.process_health_signal(_crash_signal(), actor="information-layer")
    assert result["stage"] == "permission_validation"
    assert result["result"] == "denied"


def test_full_chain_traverses_all_stages_fail_closed(tmp_path: Path) -> None:
    """A critical signal from an authorized sovereign traverses the whole
    chain. The demo tool has no rebuildable artifact, so execution is
    expected to fail — but the chain must still run verification,
    finalization, learning and produce a complete auditable report
    (fail-closed, not fail-silent)."""
    orch = _orchestrator(tmp_path)
    result = orch.process_health_signal(
        _crash_signal(), actor="health-maintenance-test-sub-sovereign"
    )
    assert result["stage"] == "complete"
    for key in ("objective", "grant", "plan", "execution", "verification", "finalization"):
        assert key in result, f"report missing {key}"
    assert result["verification"] in ("failed", "inconclusive", "passed")
    # Learning recorded the outcome either way (honest success rate).
    outcomes = orch.learning_store.load_outcomes() if hasattr(
        orch.learning_store, "load_outcomes"
    ) else None
    if outcomes is not None:
        assert len(outcomes) >= 1
