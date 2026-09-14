"""Permission/execution path behavior tests (the previously missing suite).

Covers the determinable runtime defects: authorize keyword alignment,
registry dict routing, A334 attested execution identity, sovereign MRO
status reachability, A435 codex-entry session controls, persistent
violation/delegation ledgers.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from core_system import permission_grant_ledger  # noqa: E402
from core_system.codex_decision import (  # noqa: E402
    SovereignRequest,
    accepted_outcome,
)
from governance.registries import (  # noqa: E402
    module_assignment_registry,
    validate_execution_identity,
)
from governance.sovereigns import _delegation  # noqa: E402
from governance.sovereigns._base import SovereignBase  # noqa: E402
from governance.sovereigns.permission.auth_supervision import (  # noqa: E402
    PermissionAuthSupervisionMixin,
)
from governance.sovereigns.system_runtime.module_routing import (  # noqa: E402
    SystemRuntimeModuleRoutingMixin,
)


def _request(intent: str = "permission.authorize", **payload) -> SovereignRequest:
    return SovereignRequest(
        intent=intent, subject="subject", requester="requester", payload=dict(payload)
    )


class _AuthHarness(PermissionAuthSupervisionMixin, SovereignBase):
    def __init__(self, governance) -> None:
        # Deliberately bypass SovereignBase.__init__ (no codex identity read
        # in this unit test); _governance() comes from the shared base.
        self.app = SimpleNamespace(governance=governance)
        self._governance_ref = None
        self._compliance_violations = []
        self._issued_grants = {}
        self.sovereign_id = "permission-sovereign"

    async def _adjudicate(self, request):
        raise NotImplementedError

    def _request_xingcheng_permission_review(self, payload, requester):
        return None


@pytest.mark.asyncio
async def test_authorize_kwargs_match_governance_signature(monkeypatch) -> None:
    from core_system.governance_runtime import MainSystemGovernance

    params = set(inspect.signature(MainSystemGovernance.authorize).parameters)
    assert {
        "capability",
        "action",
        "target",
        "data_scope",
        "target_tool_id",
        "target_version",
        "resource_path",
    }.issubset(params)

    calls: list[dict] = []

    class _FakeGovernance:
        def authorize(self, **kwargs):
            calls.append(kwargs)
            return {"allowed": True}

    recorded: list[dict] = []
    monkeypatch.setattr(
        "governance.sovereigns.permission.auth_supervision.record_grant",
        lambda **kwargs: recorded.append(kwargs) or 1,
    )
    harness = _AuthHarness(_FakeGovernance())
    params_payload = harness._extract_authorize_params(
        _request(capability="governed-tool-execution", target="tool:x")
    )
    assert params_payload is not None
    outcome = await harness._execute_authorization(
        harness._governance(), _request(capability="c", target="t"), params_payload
    )

    assert outcome.accepted is True
    assert calls and calls[0]["capability"] == "governed-tool-execution"
    assert recorded, "issued grant must be recorded in the ledger"


class _RoutingHarness(SystemRuntimeModuleRoutingMixin):
    def __init__(self, assignment: dict) -> None:
        self.app = SimpleNamespace()
        self._sub_sovereigns = {"child-a": object()}
        self._assignment = assignment

    def verified_basis(self, *refs):
        return ("A334",)

    async def delegate_to(self, child_id, request):
        return accepted_outcome({"delegated_to": child_id}, ("A334",))


@pytest.mark.asyncio
async def test_module_routing_reads_registry_dict_keys(monkeypatch) -> None:
    import governance.registries as registries

    harness = _RoutingHarness({"managing_sub_sovereign": "child-a"})
    monkeypatch.setattr(registries, "module_assignment", lambda module: harness._assignment)
    outcome = await harness._adjudicate_module_route(
        _request(intent="module.route", module="FILE_SORTER")
    )
    assert outcome.accepted is True
    assert outcome.result["delegated_to"] == "child-a"

    harness._assignment = {"module_architecture_code": "FILE_SORTER"}
    outcome = await harness._adjudicate_module_route(
        _request(intent="module.route", module="FILE_SORTER")
    )
    assert outcome.accepted is False
    assert outcome.refusal is not None
    assert outcome.refusal.reason_code == "SUB_SOVEREIGN_UNASSIGNED"


def test_sovereign_status_mro_prefers_mixin() -> None:
    from governance.sovereigns.decision_sovereign import DecisionSovereign
    from governance.sovereigns.permission_sovereign import PermissionSovereign
    from governance.sovereigns.synchronization_sovereign import (
        SynchronizationSovereign,
    )
    from governance.sovereigns.system_runtime_sovereign import (
        SystemRuntimeSovereign,
    )
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    for cls in (
        DecisionSovereign,
        PermissionSovereign,
        SynchronizationSovereign,
        SystemRuntimeSovereign,
    ):
        assert cls.status is not SovereignBase.status, cls.__name__
        assert cls.live_status is not SovereignBase.live_status, cls.__name__

    mro = XingchengSovereign.__mro__
    assert mro.index(SovereignBase) > mro.index(XingchengSovereign.__bases__[0])


def test_validate_execution_identity_requires_attested_value() -> None:
    rows = module_assignment_registry()
    if not rows:
        pytest.skip("module assignment registry is empty")
    row = next((r for r in rows if r.get("execution_identity")), None)
    if row is None:
        pytest.skip("no execution-identity row registered")
    code = row["module_architecture_code"]
    assert validate_execution_identity(code, row["execution_identity"]) is True
    assert validate_execution_identity(code, "unattested-echo") is False


def test_attested_execution_identity_never_echoes_module_code() -> None:
    from tasks.toolbox_execution import ExecutionMixin

    class _Harness(ExecutionMixin):
        pass

    scoped = _Harness()
    scoped.allowed_tool_ids = {"ai-assistant"}
    assert scoped._attested_execution_identity() == "ai-assistant"

    declared = _Harness()
    declared.allowed_tool_ids = {"a", "b"}
    declared.execution_identity = "git"
    assert declared._attested_execution_identity() == "git"

    ambiguous = _Harness()
    ambiguous.allowed_tool_ids = {"a", "b"}
    assert ambiguous._attested_execution_identity() == ""

    broker = _Harness()
    broker.allowed_tool_ids = None
    assert broker._attested_execution_identity() == ""


def test_official_sovereign_requires_single_use_session() -> None:
    from governance_rule.execution.codex_official import (
        mint_codex_read_session,
        official_self_declaration,
        official_sovereign,
    )

    assert (
        official_sovereign(
            "decision-sovereign",
            requester="decision-sovereign",
            purpose="self-declaration",
            provision_id="decision-sovereign",
            session_nonce="not-a-real-nonce",
        )
        is None
    )

    sovereign = official_self_declaration("decision-sovereign")
    assert sovereign is not None and sovereign.id == "decision-sovereign"

    nonce = mint_codex_read_session(
        "decision-sovereign",
        requester="decision-sovereign",
        purpose="self-declaration",
        provision_id="decision-sovereign",
    )
    first = official_sovereign(
        "decision-sovereign",
        requester="decision-sovereign",
        purpose="self-declaration",
        provision_id="decision-sovereign",
        session_nonce=nonce,
    )
    replay = official_sovereign(
        "decision-sovereign",
        requester="decision-sovereign",
        purpose="self-declaration",
        provision_id="decision-sovereign",
        session_nonce=nonce,
    )
    assert first is not None and replay is None


def test_violation_ledger_is_persistent(tmp_path: Path) -> None:
    ledger = tmp_path / "violations.jsonl"
    permission_grant_ledger.record_violation(
        sovereign_id="permission-sovereign",
        violation={"code": "A121"},
        requester="requester",
        ledger_path=ledger,
    )
    permission_grant_ledger.record_violation(
        sovereign_id="permission-sovereign",
        violation={"code": "A446"},
        requester="requester",
        ledger_path=ledger,
    )
    entries = permission_grant_ledger.load_violations(ledger)
    assert [e["violation"]["code"] for e in entries] == ["A121", "A446"]
    assert [e["sequence"] for e in entries] == [1, 2]


def test_delegation_sessions_single_use_and_receipts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        _delegation, "_DELEGATION_AUDIT_PATH", tmp_path / "delegation-audit.jsonl"
    )
    nonce = _delegation.mint_delegation("parent-sovereign", "child-sovereign", "sync")
    assert _delegation.consume_delegation(
        nonce, parent="parent-sovereign", child="child-sovereign", intent="sync"
    )
    assert not _delegation.consume_delegation(
        nonce, parent="parent-sovereign", child="child-sovereign", intent="sync"
    )
    assert not _delegation.consume_delegation(
        "unknown", parent="parent-sovereign", child="child-sovereign", intent="sync"
    )

    receipt = _delegation.mint_delegation_receipt(
        sovereign_id="decision-sovereign",
        intent="repair.decide-and-route",
        requester="worker",
        accepted=True,
        execution_mode="decision-only",
        basis=("A446",),
    )
    assert _delegation.verify_delegation_receipt(receipt) is True
    tampered = _delegation.DelegationReceipt(
        receipt_id=receipt.receipt_id,
        sovereign_id=receipt.sovereign_id,
        intent=receipt.intent,
        requester=receipt.requester,
        execution_mode="forged",
        accepted=True,
        reason_code="",
        basis=receipt.basis,
        content_hash=receipt.content_hash,
        timestamp=receipt.timestamp,
    )
    assert _delegation.verify_delegation_receipt(tampered) is False
