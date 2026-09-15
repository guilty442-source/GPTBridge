"""A446 execution pipeline tests — receipts, independent verification, audit.

Covers the previously missing implementation surface:
- universal ordered task receipts (no skipping, no missing publication),
- a universal independent verifier (never the work step),
- executor results that may not self-declare success,
- mandatory audit publication for accepted and refused commands,
- runtime-gateway audit closure and result verification.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from core_system.codex_decision import (  # noqa: E402
    SovereignRequest,
    accepted_outcome,
    refusal_outcome,
)
from governance import execution_receipts  # noqa: E402
from governance.execution_pipeline import SovereignExecutionPipeline  # noqa: E402
from governance.execution_receipts import (  # noqa: E402
    ExecutionReceiptLedger,
    ExecutionTier,
    ReceiptError,
)
from governance.independent_verifier import IndependentVerifier  # noqa: E402


class _FakeSovereign:
    """Minimal sovereign surface used by the pipeline (duck-typed)."""

    sovereign_id = "fake-sovereign"

    def __init__(
        self,
        *,
        requester_ok: bool = True,
        intent_ok: bool = True,
        outcome=None,
    ) -> None:
        self._requester_ok = requester_ok
        self._intent_ok = intent_ok
        self._outcome = outcome or accepted_outcome(
            {"execution": "none"}, ("A446", "A121")
        )
        self._verifier = IndependentVerifier()

    async def _verify_requester(self, request: SovereignRequest) -> bool:
        return self._requester_ok

    def _verify_intent(self, intent: str) -> bool:
        return self._intent_ok

    async def _adjudicate(self, request: SovereignRequest):
        return self._outcome

    async def _delegate_execution(self, decision, request):
        return self._outcome

    def verify_execution_result(self, intent: str, executor_actor: str, outcome):
        return self._verifier.verify(intent, executor_actor, outcome)


@pytest.fixture
def audit_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger = tmp_path / "sovereign_execution_audit.jsonl"
    monkeypatch.setattr(execution_receipts, "SOVEREIGN_AUDIT_LEDGER", ledger)
    return ledger


def _request(**payload) -> SovereignRequest:
    return SovereignRequest(
        intent="coordinate",
        subject="subject",
        requester="worker",
        payload=dict(payload),
    )


def test_ledger_enforces_tier_order() -> None:
    ledger = ExecutionReceiptLedger("r1", "worker")
    with pytest.raises(ReceiptError):
        ledger.record(ExecutionTier.TASK_PLANNING, "planner", "planned")


def test_ledger_forbids_self_verification() -> None:
    ledger = ExecutionReceiptLedger("r2", "worker")
    ledger.record(ExecutionTier.DISPATCH_INTAKE, "worker", "intake")
    ledger.record(ExecutionTier.AUTHORIZATION_GATE, "fake-sovereign", "allowed")
    ledger.record(ExecutionTier.TASK_PLANNING, "fake-sovereign", "planned")
    ledger.record(ExecutionTier.SPECIALIZED_EXECUTOR, "governed-executor", "executed")
    with pytest.raises(ReceiptError):
        ledger.record(
            ExecutionTier.RESULT_VERIFICATION, "governed-executor", "verified"
        )


@pytest.mark.asyncio
async def test_pipeline_accepts_only_with_complete_receipts(audit_ledger: Path) -> None:
    sovereign = _FakeSovereign()
    outcome = await SovereignExecutionPipeline(sovereign).run(_request())

    assert outcome.accepted is True
    receipts = outcome.result["execution_receipts"]
    assert receipts["complete"] is True
    assert receipts["receipts"] == len(ExecutionTier)
    assert outcome.result["verification"]["verified"] is True
    entries = audit_ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(entries) == 1
    record = json.loads(entries[0])
    assert record["request_id"]
    # The published entry carries every tier up to and including
    # result-verification; the audit-publication tier is evidenced by the
    # entry itself (see gateway receipt ``publication`` marker).
    assert len(record["receipts"]) == len(ExecutionTier) - 1
    assert record["receipts"][-1]["tier"] == "result-verification"


@pytest.mark.asyncio
async def test_pipeline_denies_unattested_execution(audit_ledger: Path) -> None:
    outcome = accepted_outcome({"execution": "exchange-repair"}, ("A446", "A121"))
    sovereign = _FakeSovereign(outcome=outcome)
    result = await SovereignExecutionPipeline(sovereign).run(_request())

    assert result.accepted is False
    assert result.refusal is not None
    assert result.refusal.reason_code == "INDEPENDENT_VERIFICATION_FAILED"
    assert result.result["execution_receipts"]["complete"] is True
    assert audit_ledger.exists()


@pytest.mark.asyncio
async def test_pipeline_denies_when_audit_unpublishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from governance import execution_pipeline

    def _boom(ledger, outcome):
        raise ReceiptError("ledger unavailable")

    monkeypatch.setattr(execution_pipeline, "publish_execution_audit", _boom)
    result = await SovereignExecutionPipeline(_FakeSovereign()).run(_request())

    assert result.accepted is False
    assert result.refusal is not None
    assert result.refusal.reason_code == "AUDIT_PUBLICATION_FAILED"


@pytest.mark.asyncio
async def test_pipeline_refusal_is_audited(audit_ledger: Path) -> None:
    result = await SovereignExecutionPipeline(
        _FakeSovereign(requester_ok=False)
    ).run(_request())

    assert result.accepted is False
    assert result.refusal is not None
    assert result.refusal.reason_code == "UNAUTHORIZED_REQUESTER"
    assert result.result["execution_receipts"]["complete"] is True
    assert audit_ledger.read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
async def test_gateway_requires_audit_sink() -> None:
    from shared_layer.runtime_gateway import InformationChannelGateway

    async def handler(command, payload):
        return {"ok": True}

    gateway = InformationChannelGateway(handler, audit=None)
    _, result = await gateway.dispatch(
        sender="ui", destination="core", command="demo.command", payload={}
    )

    assert result["ok"] is False
    assert result["error_code"] == "AUDIT_PUBLICATION_FAILED"


@pytest.mark.asyncio
async def test_gateway_verifies_result_independently() -> None:
    from shared_layer.runtime_gateway import InformationChannelGateway

    records: list[dict] = []

    async def trusted(command, payload):
        return {"ok": True, "value": 1}

    gateway = InformationChannelGateway(trusted, audit=records.append)
    _, result = await gateway.dispatch(
        sender="ui", destination="core", command="demo.command", payload={}
    )
    assert result["ok"] is True
    assert result["verification"]["verified"] is True
    assert records and records[-1]["verification"]["verified"] is True

    async def self_verified(command, payload):
        return {"ok": True, "verification": {"verified": True}}

    gateway = InformationChannelGateway(self_verified, audit=records.append)
    _, result = await gateway.dispatch(
        sender="ui", destination="core", command="demo.command", payload={}
    )
    assert result["ok"] is False
    assert result["error_code"] == "RESULT_VERIFICATION_FAILED"


@pytest.mark.asyncio
async def test_gateway_denies_when_audit_sink_raises() -> None:
    from shared_layer.runtime_gateway import InformationChannelGateway

    def broken_sink(record: dict) -> None:
        raise RuntimeError("sink down")

    async def handler(command, payload):
        return {"ok": True}

    gateway = InformationChannelGateway(handler, audit=broken_sink)
    _, result = await gateway.dispatch(
        sender="ui", destination="core", command="demo.command", payload={}
    )

    assert result["ok"] is False
    assert result["error_code"] == "AUDIT_PUBLICATION_FAILED"


def _unused_asyncio_guard() -> None:
    """Keep asyncio import meaningful for environments without async markers."""
    asyncio.get_event_loop_policy()
