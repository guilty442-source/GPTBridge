"""Convergence framework tests (staged copies only; live codex read-only).

Covers the successor pipeline skeleton (stage/apply/validate/publish-guard),
the version-axis and projection reports, closure evaluation and the
``RULE_CAPABILITY_DISPATCH_V1`` evaluator skeleton.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_rule.execution.convergence import (  # noqa: E402
    ConvergenceError,
    apply_re_tiering,
    compute_closures,
    evaluate_dispatch,
    load_re_tiering_plan,
    projection_status,
    publish,
    stage_copy,
    validate_module_capability,
    validate_staged,
    version_axis_report,
)

LIVE = ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"


def _module(**overrides):
    base = {
        "module_identity": "file-sorter",
        "single_responsibility": "organize files",
        "capability_codes": ["file-organize"],
        "resource_requirements": {"memory_mb": 256},
        "permission_requirements": ["filesystem.write"],
        "contract_version": "1",
        "runtime_state": "active",
        "availability": "ready",
        "version": "1.0.0",
        "owner_engine_domain": "system-runtime",
        "execution_identity": "file-sorter",
    }
    base.update(overrides)
    return base


def _dispatch_facts(**overrides):
    facts = {
        "requirements": {
            "capability_codes": ["file-organize"],
            "permission_scope": ["filesystem.write"],
            "resource_constraints": {"memory_mb": 512},
            "contract_version": "1",
        },
        "candidates": [_module()],
        "lease": {"valid": True, "fencing_token": 7},
        "decision": {"recorded": True},
        "selected_by_module": False,
    }
    facts.update(overrides)
    return facts


def test_dispatch_eligible_module_passes() -> None:
    passed, decision, reason = evaluate_dispatch(_dispatch_facts())
    assert passed and decision == "PASS" and "file-sorter" in reason


def test_dispatch_conflict_fails_closed() -> None:
    passed, decision, _ = evaluate_dispatch(
        _dispatch_facts(candidates=[_module(), _module(module_identity="other", execution_identity="other")])
    )
    assert not passed and decision == "FAIL_CLOSED"


def test_dispatch_no_candidate_fails_closed() -> None:
    passed, decision, _ = evaluate_dispatch(_dispatch_facts(candidates=[]))
    assert not passed and decision == "FAIL_CLOSED"


def test_dispatch_self_selection_fails_closed() -> None:
    passed, decision, _ = evaluate_dispatch(_dispatch_facts(selected_by_module=True))
    assert not passed and decision == "FAIL_CLOSED"


def test_dispatch_missing_lease_fails_closed() -> None:
    passed, decision, _ = evaluate_dispatch(_dispatch_facts(lease={"valid": False}))
    assert not passed and decision == "FAIL_CLOSED"


def test_module_capability_schema_validation() -> None:
    assert validate_module_capability(_module()) == []
    assert validate_module_capability({"module_identity": "x"})


def test_stage_copy_applies_plan_and_validates(tmp_path: Path) -> None:
    staged = stage_copy(LIVE, staging_root=tmp_path)
    assert staged.path.is_file()
    plan = load_re_tiering_plan()
    result = apply_re_tiering(staged.connect(), plan, version="2026-09-21T00:00:00Z")
    assert result.applied > 0
    assert validate_staged(staged) == ()
    with pytest.raises(ConvergenceError):
        publish(staged)  # approve required


def test_version_axis_and_projection_reports() -> None:
    axes = version_axis_report(LIVE)
    assert axes["codex_version"] and axes["current_version"]
    projections = projection_status(LIVE)
    assert projections["codex_version"] == axes["codex_version"]
    closures = compute_closures(LIVE)
    assert closures["VERSION_CURRENTNESS_CLOSURE"] in {"PASS", "INCOMPLETE_EVIDENCE", "FAIL"}
    assert closures["MIRROR_QUALITY_CLOSURE"] == "PASS"
    assert closures["MACHINE_SCHEMA_PARITY_CLOSURE"] == "INCOMPLETE_EVIDENCE"

    # W4-3 residual: contract/registry artifacts feed the closure evaluator.
    assert closures["CONTRACT_REGISTRY_CLOSURE"] in {"PASS", "INCOMPLETE_EVIDENCE", "FAIL"}
    # Current state: two governance-topology contracts are partially-implemented.
    assert closures["CONTRACT_REGISTRY_CLOSURE"] == "INCOMPLETE_EVIDENCE"


def test_live_codex_untouched_by_framework() -> None:
    import hashlib

    before = hashlib.sha256(LIVE.read_bytes()).hexdigest()
    plan = load_re_tiering_plan()
    assert plan["entries"]
    assert hashlib.sha256(LIVE.read_bytes()).hexdigest() == before
