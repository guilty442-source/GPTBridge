"""Five-sovereign audit gate for the automated Codex update flow."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Iterator

import pytest

from governance_rule.execution.codex_amendment_audit_gate import (
    AMENDMENT_TEST_SUITE_BUDGET_SECONDS,
    CERTIFICATE_SCHEMA,
    DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS,
    SOVEREIGN_AUDIT_SPECS,
    SOVEREIGN_IDS,
    CodexAmendmentAuditGate,
    build_xingcheng_assistant_core_check,
)


@pytest.fixture(scope="module", autouse=True)
def _amendment_gate_suite_runtime_budget() -> Iterator[None]:
    """Enforce the governor's 20-second budget for this governed test suite.

    The budget is measured on a monotonic clock across this module's tests;
    a suite slower than the budget reports a teardown failure, so an
    over-budget amendment gate suite is never a pass.
    """
    started = time.monotonic()
    yield
    elapsed = time.monotonic() - started
    if elapsed > AMENDMENT_TEST_SUITE_BUDGET_SECONDS:
        pytest.fail(
            "amendment gate test suite budget exceeded: "
            f"{elapsed:.3f}s > {AMENDMENT_TEST_SUITE_BUDGET_SECONDS:.0f}s"
        )


def _passing_checks() -> dict[str, object]:
    return {
        "decision-sovereign": lambda: {
            "ok": True,
            "method": "decision-precedence-audit",
            "findings": ["no conflicting successor"],
            "evidence": {
                "amendment_class": "ARCHITECTURE_AUTHORITY",
                "basis_references": ["A377", "A382"],
                "successor_scope_unique": True,
            },
        },
        "permission-sovereign": lambda: {
            "ok": True,
            "method": "directory-identity-audit",
            "findings": ["identity/lifecycle parity"],
            "evidence": {
                "directory_rows": ["project_architecture_directory"],
                "identity_lifecycle_parity": True,
                "testflow_references": ["TS_SHARED_LAYER"],
            },
        },
        "system-runtime-sovereign": lambda: {
            "ok": True,
            "method": "runtime-reader-audit",
            "findings": ["readers pinned to generation"],
            "evidence": {
                "reader_generation_plan": "drain-then-publish",
                "channel_continuity": True,
                "health_window": 300,
            },
        },
        "automation-sovereign": lambda: {
            "ok": True,
            "method": "staging-seal-audit",
            "findings": ["seal roots recomputed"],
            "evidence": {
                "staging_isolation": True,
                "seal_roots": {
                    "content_root": "content",
                    "identity_root": "identity",
                    "full_root": "full",
                },
                "mirror_chain": True,
                "version_identity": "E2:next",
                "rollback_pointer": True,
            },
        },
        "xingcheng": build_xingcheng_assistant_core_check(
            "codex-amend-test", ["registry:formal_rule_registry"]
        ),
    }


@pytest.mark.asyncio
async def test_all_five_sovereigns_pass_releases_division(tmp_path: Path) -> None:
    ledger = tmp_path / "codex_amendment_audit.jsonl"
    gate = CodexAmendmentAuditGate(ledger_path=ledger)

    result = await gate.audit(
        "codex-amend-test-1",
        _passing_checks(),
        predecessor={
            "codex_version": "2026-09-16T14:48:49Z",
            "history_head": "44a6810a",
        },
    )

    assert result.ok is True
    assert result.reason == "ALL_FIVE_SOVEREIGNS_AUDITED"
    assert result.audit_recorded is True
    assert result.division_plan is not None
    owners = {item["owner"] for item in result.division_plan}
    assert "human-governor" not in owners
    assert "automation-sovereign" in owners
    assert "xingcheng-assistant" in owners
    assert [receipt.sovereign_id for receipt in result.receipts] == list(SOVEREIGN_IDS)
    certificate = result.certificate
    assert certificate is not None
    assert certificate["schema"] == CERTIFICATE_SCHEMA
    assert certificate["verdict"] == "FIVE_SOVEREIGN_AUDIT_PASSED"
    assert certificate["predecessor"]["codex_version"] == "2026-09-16T14:48:49Z"
    assert len(certificate["receipts"]) == len(SOVEREIGN_IDS)
    assert len(certificate["certificate_hash"]) == 64
    assert result.budget_ms == int(DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS * 1000)
    assert 0 <= result.duration_ms < result.budget_ms
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["ok"] is True
    assert entry["division_released"] is True
    assert entry["certificate_issued"] is True
    assert entry["duration_ms"] == result.duration_ms
    assert entry["budget_ms"] == result.budget_ms


@pytest.mark.asyncio
async def test_one_failed_audit_denies_all_division(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks["permission-sovereign"] = lambda: {
        "ok": False,
        "error": "DIRECTORY_PARITY_MISMATCH",
        "method": "directory-identity-audit",
    }
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit("codex-amend-test-2", checks)

    assert result.ok is False
    assert result.reason == "SOVEREIGN_AUDIT_FAILED"
    assert result.failed == ("permission-sovereign",)
    assert result.division_plan is None
    assert result.certificate is None


@pytest.mark.asyncio
async def test_missing_sovereign_audit_is_fail_closed(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks.pop("xingcheng")
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit("codex-amend-test-3", checks)

    assert result.ok is False
    assert result.reason == "MISSING_SOVEREIGN_AUDIT"
    assert result.missing == ("xingcheng",)
    assert result.receipts == ()
    assert result.division_plan is None
    assert result.audit_recorded is True


@pytest.mark.asyncio
async def test_unknown_audit_actor_is_rejected(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks["mallory"] = lambda: {"ok": True}
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit("codex-amend-test-4", checks)

    assert result.ok is False
    assert result.reason == "UNKNOWN_AUDIT_ACTOR"
    assert result.failed == ("mallory",)


@pytest.mark.asyncio
async def test_xingcheng_assistant_core_audit_requires_no_network(
    tmp_path: Path,
) -> None:
    checks = _passing_checks()
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit("codex-amend-test-5", checks)
    xingcheng = next(
        receipt for receipt in result.receipts if receipt.sovereign_id == "xingcheng"
    )
    assert result.ok is True
    assert xingcheng.network_search is False
    assert xingcheng.method == "xingcheng-assistant-core-audit"
    assert len(xingcheng.evidence_hash) == 64


def test_xingcheng_assistant_core_check_fails_closed_without_scope() -> None:
    check = build_xingcheng_assistant_core_check("", [])
    outcome = check()
    assert outcome["ok"] is False
    assert outcome["error"] == "ASSISTANT_CORE_SCOPE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_unrecordable_audit_is_not_a_pass(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    gate = CodexAmendmentAuditGate(ledger_path=blocker / "audit.jsonl")

    result = await gate.audit("codex-amend-test-6", _passing_checks())

    assert result.ok is False
    assert result.reason == "AUDIT_RECORD_FAILED"
    assert result.division_plan is None


def test_sovereign_audit_charters_are_explicit() -> None:
    assert len(SOVEREIGN_AUDIT_SPECS) == len(SOVEREIGN_IDS) == 5
    for spec in SOVEREIGN_AUDIT_SPECS:
        assert spec.duties
        assert spec.required_evidence
        assert spec.forbidden
        assert spec.post_audit_duties
        assert spec.owner_sub_sovereign
    owners = {spec.owner_sub_sovereign for spec in SOVEREIGN_AUDIT_SPECS}
    # A592: sub-sovereign 層已退役，duties 歸 owning core engine（A591）。
    assert owners == {
        "decision-sovereign",
        "permission-sovereign",
        "system-runtime-sovereign",
        "automation-sovereign",
        "xingcheng-assistant",
    }


def test_runtime_budgets_match_the_governor_amendment() -> None:
    assert DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS == 30.0
    assert AMENDMENT_TEST_SUITE_BUDGET_SECONDS == 20.0


@pytest.mark.asyncio
async def test_flow_deadline_denies_slow_checks_fail_closed(
    tmp_path: Path,
) -> None:
    async def slow_check() -> dict[str, object]:
        await asyncio.sleep(0.5)
        return {"ok": True, "method": "slow-audit"}

    checks = _passing_checks()
    checks["decision-sovereign"] = slow_check
    ledger = tmp_path / "codex_amendment_audit.jsonl"
    gate = CodexAmendmentAuditGate(flow_deadline_seconds=0.05, ledger_path=ledger)

    result = await gate.audit("codex-amend-budget-1", checks)

    assert result.ok is False
    assert result.reason == "AUDIT_FLOW_DEADLINE_EXCEEDED"
    assert result.division_plan is None
    assert result.certificate is None
    assert result.budget_ms == 50
    assert result.duration_ms < result.budget_ms + 1000
    assert result.audit_recorded is True
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["reason"] == "AUDIT_FLOW_DEADLINE_EXCEEDED"
    assert entry["division_released"] is False
    assert entry["certificate_issued"] is False
    assert entry["budget_ms"] == 50


@pytest.mark.asyncio
async def test_blocking_check_past_budget_is_denied_post_hoc(
    tmp_path: Path,
) -> None:
    def blocking_check() -> dict[str, object]:
        time.sleep(0.2)
        return {
            "ok": True,
            "method": "blocking-audit",
            "evidence": {
                "amendment_class": "ARCHITECTURE_AUTHORITY",
                "basis_references": ["A537"],
                "successor_scope_unique": True,
            },
        }

    checks = _passing_checks()
    checks["decision-sovereign"] = blocking_check
    gate = CodexAmendmentAuditGate(
        flow_deadline_seconds=0.05, ledger_path=tmp_path / "audit.jsonl"
    )

    result = await gate.audit("codex-amend-budget-2", checks)

    assert result.ok is False
    assert result.reason == "AUDIT_FLOW_DEADLINE_EXCEEDED"
    assert result.division_plan is None
    assert result.duration_ms >= result.budget_ms


@pytest.mark.asyncio
async def test_unknown_requester_is_denied(tmp_path: Path) -> None:
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit(
        "codex-amend-requester-1", _passing_checks(), requester="mallory"
    )

    assert result.ok is False
    assert result.reason == "UNKNOWN_AMENDMENT_REQUESTER"
    assert result.division_plan is None
    assert result.certificate is None


@pytest.mark.asyncio
async def test_requester_audits_its_own_standard_duty_procedure(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "audit.jsonl"
    gate = CodexAmendmentAuditGate(ledger_path=ledger)

    result = await gate.audit(
        "codex-amend-requester-2",
        _passing_checks(),
        requester="decision-sovereign",
    )

    assert result.ok is True
    assert result.requester == "decision-sovereign"
    assert result.self_audit_passed is True
    assert result.division_plan is not None
    receipt = next(
        item for item in result.receipts if item.sovereign_id == "decision-sovereign"
    )
    assert receipt.ok is True
    assert receipt.method == "decision-precedence-audit"
    assert receipt.error == ""
    assert [item.sovereign_id for item in result.receipts][0] == "decision-sovereign"
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["requester"] == "decision-sovereign"
    assert entry["self_audit_passed"] is True
    assert entry["ok"] is True


@pytest.mark.asyncio
async def test_originator_self_audit_failure_does_not_dispatch_others(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    base = _passing_checks()
    checks: dict[str, object] = {}
    for sovereign_id, check in base.items():
        def make(sid: str = sovereign_id, fn: object = check) -> object:
            def runner() -> object:
                calls.append(sid)
                return fn()

            return runner

        checks[sovereign_id] = make()

    def failing_self_audit() -> dict[str, object]:
        calls.append("decision-sovereign")
        return {
            "ok": False,
            "method": "decision-precedence-audit",
            "error": "SELF_AUDIT_FINDING",
        }

    checks["decision-sovereign"] = failing_self_audit
    ledger = tmp_path / "audit.jsonl"
    gate = CodexAmendmentAuditGate(ledger_path=ledger)

    result = await gate.audit(
        "codex-amend-requester-5",
        checks,
        requester="decision-sovereign",
    )

    assert result.ok is False
    assert result.reason == "ORIGINATOR_SELF_AUDIT_FAILED"
    assert result.self_audit_passed is False
    assert result.failed == ("decision-sovereign",)
    assert len(result.receipts) == 1
    assert calls == ["decision-sovereign"]
    assert result.division_plan is None
    assert result.certificate is None
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["reason"] == "ORIGINATOR_SELF_AUDIT_FAILED"
    assert entry["self_audit_passed"] is False


@pytest.mark.asyncio
async def test_requester_independent_verifier_is_recorded(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks["decision-sovereign"] = lambda: {
        "ok": True,
        "method": "decision-precedence-audit",
        "independent_verifier": "xingcheng",
        "findings": ["proposer disclosure independently challenged"],
        "evidence": {
            "amendment_class": "ARCHITECTURE_AUTHORITY",
            "basis_references": ["A377", "A382"],
            "successor_scope_unique": True,
        },
    }
    ledger = tmp_path / "audit.jsonl"
    gate = CodexAmendmentAuditGate(ledger_path=ledger)

    result = await gate.audit(
        "codex-amend-requester-3",
        checks,
        requester="decision-sovereign",
    )

    assert result.ok is True
    assert result.requester == "decision-sovereign"
    assert result.requester_independent_verifier == "xingcheng"
    assert result.self_audit_passed is True
    assert result.division_plan is not None
    assert result.certificate is not None
    assert result.certificate["requester"] == "decision-sovereign"
    decision = next(
        item
        for item in result.certificate["receipts"]
        if item["sovereign_id"] == "decision-sovereign"
    )
    assert decision["independent_verifier"] == "xingcheng"
    entry = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert entry["requester"] == "decision-sovereign"
    assert entry["requester_independent_verifier"] == "xingcheng"
    assert entry["division_released"] is True


@pytest.mark.asyncio
async def test_xingcheng_alias_requester_is_normalized(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks["xingcheng"] = lambda: {
        "ok": True,
        "method": "xingcheng-assistant-core-audit",
        "independent_verifier": "permission-sovereign",
        "evidence": {
            "assistant_core_review": "deterministic-standard-charter",
            "scope_classification": ["registry:formal_rule_registry"],
            "redaction_check": "metadata-only",
        },
    }
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit(
        "codex-amend-requester-4", checks, requester="星澄"
    )

    assert result.ok is True
    assert result.requester == "xingcheng"
    assert result.requester_independent_verifier == "permission-sovereign"


@pytest.mark.asyncio
async def test_missing_required_evidence_fails_closed(tmp_path: Path) -> None:
    checks = _passing_checks()
    checks["decision-sovereign"] = lambda: {
        "ok": True,
        "method": "decision-precedence-audit",
    }
    gate = CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")

    result = await gate.audit("codex-amend-test-7", checks)

    assert result.ok is False
    assert result.failed == ("decision-sovereign",)
    receipt = next(
        item for item in result.receipts if item.sovereign_id == "decision-sovereign"
    )
    assert receipt.error.startswith("AUDIT_EVIDENCE_INCOMPLETE:")
    assert result.certificate is None
    assert result.division_plan is None
