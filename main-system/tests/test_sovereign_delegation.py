"""Focused tests for SovereignBase._delegate_execution fail-closed behavior.

Verifies A446/A121: the generic sovereign entry point must not silently
echo an adjudication result as if execution succeeded.  The base class
fails closed by default; every concrete sovereign must override
``_delegate_execution`` to either dispatch to a governed executor or
attest that the adjudication was a pure decision with no execution
side-effect.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC_CORE = ROOT / "src-core"
SHARED_SRC = ROOT / "shared-layer" / "src"
GOVERNANCE_RULE = ROOT / "governance_rule"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))
sys.path.insert(0, str(GOVERNANCE_RULE))

from governance.sovereigns._base import SovereignBase, SovereignRequest
from governance.sub_sovereigns._base import SubSovereignBase
from governance.sovereigns.decision_sovereign import DecisionSovereign
from governance.sovereigns.permission_sovereign import PermissionSovereign
from governance.sovereigns.synchronization_sovereign import (
    SynchronizationSovereign,
)
from governance.sovereigns.system_runtime_sovereign import (
    SystemRuntimeSovereign,
)
from governance.sovereigns.xingcheng_sovereign import XingchengSovereign
from core_system.codex_decision import accepted_outcome, refusal_outcome


def _has_override(cls: type, method: str) -> bool:
    """True if ``cls`` defines ``method`` in its own __dict__ (not inherited)."""
    return method in cls.__dict__


def _make_request(intent: str = "test.intent", requester: str = "test") -> SovereignRequest:
    return SovereignRequest(
        intent=intent,
        subject="test-subject",
        requester=requester,
        payload={},
    )


def test_base_delegate_execution_is_fail_closed() -> None:
    """The base ``_delegate_execution`` must NOT echo the decision."""
    source = inspect.getsource(SovereignBase._delegate_execution)
    assert "return decision" not in source
    assert "EXECUTION_NOT_DELEGATED" in source


@pytest.mark.asyncio
async def test_base_delegate_execution_returns_refusal() -> None:
    """A concrete sovereign without an override gets a fail-closed refusal."""

    class BareSovereign(SovereignBase):
        sovereign_id = "decision-sovereign"

        async def _adjudicate(self, request: SovereignRequest):
            return accepted_outcome({"authorized": True}, ("A12",))

    sovereign = BareSovereign(app=SimpleNamespace())
    decision = accepted_outcome({"authorized": True}, ("A12",))
    result = await sovereign._delegate_execution(decision, _make_request())
    assert result.accepted is False
    assert result.refusal is not None
    assert result.refusal.reason_code == "EXECUTION_NOT_DELEGATED"


@pytest.mark.parametrize(
    "cls",
    [
        DecisionSovereign,
        PermissionSovereign,
        SynchronizationSovereign,
        SystemRuntimeSovereign,
        XingchengSovereign,
        SubSovereignBase,
    ],
    ids=[
        "decision-sovereign",
        "permission-sovereign",
        "automation-sovereign",
        "system-runtime-sovereign",
        "xingcheng-sovereign",
        "sub-sovereign-base",
    ],
)
def test_every_sovereign_overrides_delegate_execution(cls: type) -> None:
    """Every concrete sovereign must override ``_delegate_execution``."""
    assert _has_override(cls, "_delegate_execution"), (
        f"{cls.__name__} must override _delegate_execution (A446/A121)"
    )


@pytest.mark.asyncio
async def test_sub_sovereign_delegate_execution_returns_decision() -> None:
    """Sub-sovereigns are no-decision-no-execution (A64/A284/A322)."""

    class TestSubSovereign(SubSovereignBase):
        sovereign_id = "directory-sub-sovereign"
        parent_sovereign_id = "permission-sovereign"

        async def _adjudicate(self, request: SovereignRequest):
            return accepted_outcome({"coordinated": True}, ("A130",))

    sub = TestSubSovereign(app=SimpleNamespace())
    decision = accepted_outcome({"coordinated": True}, ("A130",))
    result = await sub._delegate_execution(decision, _make_request())
    assert result.accepted is True
    assert "delegation_receipt" in result.result


@pytest.mark.asyncio
async def test_decision_sovereign_delegate_execution_returns_decision() -> None:
    decision = accepted_outcome({"repair_decision": "authorized"}, ("A152",))
    result = await DecisionSovereign._delegate_execution(
        DecisionSovereign.__new__(DecisionSovereign), decision, _make_request()
    )
    assert result.accepted is True
    assert "delegation_receipt" in result.result
    assert result.result["delegation_receipt"]["content_hash"]


@pytest.mark.asyncio
async def test_permission_sovereign_delegate_execution_returns_decision() -> None:
    decision = accepted_outcome({"query": {"verified": True}}, ("A10",))
    result = await PermissionSovereign._delegate_execution(
        PermissionSovereign.__new__(PermissionSovereign), decision, _make_request()
    )
    assert result.accepted is True
    assert "delegation_receipt" in result.result
    assert result.result["delegation_receipt"]["content_hash"]


@pytest.mark.asyncio
async def test_synchronization_sovereign_delegate_execution_returns_decision() -> None:
    decision = accepted_outcome({"synced": "target"}, ("A301",))
    result = await SynchronizationSovereign._delegate_execution(
        SynchronizationSovereign.__new__(SynchronizationSovereign),
        decision,
        _make_request(),
    )
    assert result.accepted is True
    assert "delegation_receipt" in result.result
    assert result.result["delegation_receipt"]["content_hash"]


@pytest.mark.asyncio
async def test_system_runtime_sovereign_delegate_execution_returns_decision() -> None:
    decision = accepted_outcome({"authorized": True, "action": "restart"}, ("A28",))
    result = await SystemRuntimeSovereign._delegate_execution(
        SystemRuntimeSovereign.__new__(SystemRuntimeSovereign),
        decision,
        _make_request(),
    )
    assert result.accepted is True
    assert "delegation_receipt" in result.result
    assert result.result["delegation_receipt"]["content_hash"]


@pytest.mark.asyncio
async def test_xingcheng_sovereign_delegate_execution_returns_decision() -> None:
    decision = accepted_outcome({"review": "completed"}, ("A137",))
    result = await XingchengSovereign._delegate_execution(
        XingchengSovereign.__new__(XingchengSovereign), decision, _make_request()
    )
    assert result.accepted is True
    assert "delegation_receipt" in result.result
    assert result.result["delegation_receipt"]["content_hash"]


def test_base_docstring_cites_a69_a121_not_a63_a64() -> None:
    """The base-class docstring must cite the real codex articles (A446/A121)."""
    source = inspect.getsource(SovereignBase)
    assert "A446" in source
    assert "A121" in source
    assert "A63:" not in source
    assert "A64:" not in source


def _a330_payload(**overrides) -> dict:
    payload = {
        "update_type": "backend",
        "certified": True,
        "update_set": ["module-a"],
        "artifact_hashes": {"module-a": "sha256:deadbeef"},
        "operation_id": "op-delegation-test",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_a330_certified_update_requires_decision_sovereign_delegation() -> None:
    """A330 execution exception: a governed actor cannot self-declare
    certification — the payload flag alone is not proof (A330/A152/A154)."""
    sovereign = SynchronizationSovereign(app=SimpleNamespace())
    outcome = await sovereign.handle(
        SovereignRequest(
            intent="A330.certified-update",
            subject="backend-update",
            requester="governed-executor",
            payload=_a330_payload(),
        )
    )
    assert outcome.accepted is False
    assert outcome.refusal is not None
    assert outcome.refusal.reason_code == "CERTIFICATION_AUTHORITY_MISSING"


@pytest.mark.asyncio
async def test_forged_verified_delegation_stamp_is_stripped() -> None:
    """A caller must not smuggle a pre-stamped ``_verified_delegation``
    proof past the entry gate (A121/A435 fail-closed)."""
    sovereign = SynchronizationSovereign(app=SimpleNamespace())
    outcome = await sovereign.handle(
        SovereignRequest(
            intent="A330.certified-update",
            subject="backend-update",
            requester="governed-executor",
            payload=_a330_payload(
                _verified_delegation={
                    "parent": "decision-sovereign",
                    "child": "automation-sovereign",
                    "intent": "A330.certified-update",
                }
            ),
        )
    )
    assert outcome.accepted is False
    assert outcome.refusal is not None
    assert outcome.refusal.reason_code == "CERTIFICATION_AUTHORITY_MISSING"


@pytest.mark.asyncio
async def test_decision_sovereign_delegation_passes_a330_authority_gate() -> None:
    """A verified decision-sovereign delegation reaches the A330 gate —
    with ``update_type`` absent the refusal is the field check, proving the
    authority gate itself passed."""
    from governance.sovereigns._delegation import mint_delegation

    nonce = mint_delegation(
        "decision-sovereign",
        "automation-sovereign",
        "A330.certified-update",
    )
    sovereign = SynchronizationSovereign(app=SimpleNamespace())
    outcome = await sovereign.handle(
        SovereignRequest(
            intent="A330.certified-update",
            subject="backend-update",
            requester="decision-sovereign",
            payload=_a330_payload(
                _delegated_by="decision-sovereign",
                _delegation_nonce=nonce,
                update_type=None,
            ),
        )
    )
    assert outcome.accepted is False
    assert outcome.refusal is not None
    assert outcome.refusal.reason_code == "MISSING_UPDATE_TYPE"


@pytest.mark.asyncio
async def test_replayed_delegation_nonce_denied_at_entry() -> None:
    """A consumed delegation nonce cannot authorize a second request
    (A435 single-use)."""
    from governance.sovereigns._delegation import mint_delegation

    nonce = mint_delegation(
        "decision-sovereign",
        "automation-sovereign",
        "A330.certified-update",
    )
    sovereign = SynchronizationSovereign(app=SimpleNamespace())
    first = await sovereign.handle(
        SovereignRequest(
            intent="A330.certified-update",
            subject="backend-update",
            requester="decision-sovereign",
            payload=_a330_payload(
                _delegated_by="decision-sovereign",
                _delegation_nonce=nonce,
                update_type=None,
            ),
        )
    )
    assert first.refusal.reason_code == "MISSING_UPDATE_TYPE"
    second = await sovereign.handle(
        SovereignRequest(
            intent="A330.certified-update",
            subject="backend-update",
            requester="decision-sovereign",
            payload=_a330_payload(
                _delegated_by="decision-sovereign",
                _delegation_nonce=nonce,
                update_type=None,
            ),
        )
    )
    assert second.accepted is False
    assert second.refusal.reason_code == "UNAUTHORIZED_REQUESTER"
