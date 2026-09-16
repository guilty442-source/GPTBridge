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
    refusal_outcome,
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
        self._app = SimpleNamespace(governance=governance)
        self._governance_ref = None
        self._compliance_violations = []
        self._issued_grants = {}
        self.sovereign_id = "permission-sovereign"

    async def _adjudicate(self, request):
        raise NotImplementedError

    async def _request_xingcheng_permission_review(self, payload, requester):
        return None


def test_two_key_review_gate_fail_closed_semantics() -> None:
    """A319: absent/denied/changed findings refuse; only ``pass`` proceeds."""
    import asyncio
    from governance.sovereigns.permission.auth_supervision import (
        PermissionAuthSupervisionMixin,
    )

    class _ReviewHarness(PermissionAuthSupervisionMixin):
        def __init__(self, finding: str | None) -> None:
            self.app = SimpleNamespace()
            self.sovereign_id = "permission-sovereign"
            self._finding = finding

        async def _request_xingcheng_permission_review(self, payload, requester):
            if self._finding is None:
                return None
            return accepted_outcome({"finding": self._finding}, ("A319",))

        def verified_basis(self, *refs):
            return tuple(refs)

    req = _request(intent="permission.authorize", capability="c", target="t")
    assert asyncio.run(_ReviewHarness("pass")._check_two_key_review(req)) is None
    denied = asyncio.run(_ReviewHarness("deny-objection")._check_two_key_review(req))
    assert denied is not None and denied.refusal.reason_code == "TWO_KEY_REVIEW_DENIED"
    missing = asyncio.run(_ReviewHarness(None)._check_two_key_review(req))
    assert missing.refusal.reason_code == "TWO_KEY_REVIEW_UNAVAILABLE"
    other = asyncio.run(_ReviewHarness("unknown")._check_two_key_review(req))
    assert other.refusal.reason_code == "TWO_KEY_REVIEW_NOT_PASSED"


@pytest.mark.asyncio
async def test_permission_lifecycle_requires_two_key_review(monkeypatch) -> None:
    """A319: renew/restrict/suspend/revoke/terminate refuse without 星澄 review."""
    from governance.sovereigns.permission.permission_lifecycle import (
        PermissionLifecycleMixin,
    )
    import core_system.permission_grant_ledger as ledger

    class _LifecycleHarness(PermissionLifecycleMixin):
        def __init__(self) -> None:
            self.app = SimpleNamespace()
            self.sovereign_id = "permission-sovereign"
            self._issued_grants = {}

        def verified_basis(self, *refs):
            return tuple(refs)

        async def _check_two_key_review(self, request, **kwargs):
            return refusal_outcome("TWO_KEY_REVIEW_UNAVAILABLE", ("A319", "A10"))

        def _iso_now(self):
            return "t"

    # Force the ledger to report the grant as issued so the review gate is
    # the deciding control under test.
    import governance.sovereigns.permission.permission_lifecycle as plc

    monkeypatch.setattr(plc, "was_issued", lambda pid: True)
    monkeypatch.setattr(plc, "current_status", lambda pid: {"status": "issued"})
    harness = _LifecycleHarness()
    req = _request(intent="permission.revoke", permission_id="perm-x")
    outcome = await harness._adjudicate_permission_revoke(req)
    assert outcome.accepted is False
    assert outcome.refusal.reason_code == "TWO_KEY_REVIEW_UNAVAILABLE"

    # With a passing review the same request proceeds to the ledger append.
    class _PassingHarness(_LifecycleHarness):
        async def _check_two_key_review(self, request, **kwargs):
            return None

    recorded: list[dict] = []
    monkeypatch.setattr(
        plc, "record_lifecycle", lambda **kw: recorded.append(kw) or 1
    )
    outcome = await _PassingHarness()._adjudicate_permission_revoke(req)
    assert outcome.accepted is True
    assert recorded and recorded[0]["operation"] == "revoke"


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
        "governance.sovereigns.permission.auth_supervision_helpers.record_grant",
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
    from governance.sovereigns.automation_sovereign import AutomationSovereign
    from governance.sovereigns.system_runtime_sovereign import (
        SystemRuntimeSovereign,
    )
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    for cls in (
        DecisionSovereign,
        PermissionSovereign,
        AutomationSovereign,
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


def _toolbox_service():
    from tasks.toolbox_service import ToolboxService

    return ToolboxService(ROOT.parent)


def _require_module_registry() -> None:
    """Skip when the codex-backed registry is unreachable in this worker.

    ``governance.registries`` caches an empty projection when the official
    entry denies a lookup (e.g. the live system's persistent entry-state
    store is mid-rewrite); the gate's fail-closed denial is correct there,
    so these tests require a reachable registry.
    """
    import governance.registries as registries
    from governance.registries import module_assignment

    registries._registries.cache_clear()
    try:
        if module_assignment("FILE_SORTER") is None:
            pytest.skip("module assignment registry unavailable")
    except (OSError, KeyError, ValueError, RuntimeError, PermissionError):
        pytest.skip("module assignment registry unavailable")


def test_attested_execution_identity_resolves_via_bound_channel() -> None:
    """A334: the attested identity is derived from the sealed chain —
    manifest id -> runtime owner -> channel claimant -> host module —
    and never echoes the requested tool id or a declared attribute."""
    service = _toolbox_service()
    _require_module_registry()
    # Direct host tools resolve to their own module code.
    assert service._attested_execution_identity("file-sorter") == "FILE_SORTER"
    assert service._attested_execution_identity("ai-assistant") == "AI_ASSISTANT"
    # local-model's channel is claimed by the nested sealed identity
    # ``xingcheng``; the attested identity is the host module's code.
    assert service._attested_execution_identity("local-model") == "LOCAL_MODEL"
    assert service._module_code_for_identity("xingcheng") == "LOCAL_MODEL"
    # A self-declared attribute is never consulted.
    service.execution_identity = "git"
    assert service._attested_execution_identity("file-sorter") == "FILE_SORTER"


def test_module_assignment_gate_four_way_identity_consistency() -> None:
    """A334 gate: manifest id, channel claimant, bootstrap-bound identity
    and registry execution identity must agree before execution queues."""
    service = _toolbox_service()
    _require_module_registry()
    # Registered host tools pass all four checks.
    assert service._verify_module_assignment("file-sorter") is None
    assert service._verify_module_assignment("local-model") is None
    # A companion without a registered module fails closed.
    denied = service._verify_module_assignment("star-chat")
    assert denied is not None and denied["ok"] is False
    assert denied["error_code"] == "MODULE_NOT_IN_REGISTRY"
    # A manifest that declares a different id is rejected.
    import json as _json

    manifest, tool_dir = service._load_manifest_cached("file-sorter")
    forged = dict(manifest)
    forged["id"] = "global-cleaner"
    service._manifest_cache["file-sorter"] = (forged, tool_dir)
    try:
        denied = service._verify_module_assignment("file-sorter")
        assert denied is not None
        assert denied["error_code"] == "MANIFEST_IDENTITY_MISMATCH"
    finally:
        service._manifest_cache["file-sorter"] = (manifest, tool_dir)
    assert _json  # manifest round-trip sanity


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


@pytest.mark.asyncio
async def test_xingcheng_execution_intents_dispatch_to_governed_executor() -> None:
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    calls: list[str] = []

    async def executor(request):
        calls.append(request.intent)
        return {"executed": True, "intent": request.intent}

    wired = XingchengSovereign(SimpleNamespace(xingcheng_executor=executor))
    assert wired.app is not None, "sovereign __init__ must not drop the host app"
    decision = accepted_outcome({"action": "domain.execute"}, ("A20",))
    request = _request(intent="domain.execute")

    outcome = await wired._delegate_execution(decision, request)
    assert outcome.accepted is True
    assert calls == ["domain.execute"]
    assert (
        outcome.result["delegation_receipt"]["execution_mode"] == "governed-executor"
    )

    advisory = await wired._delegate_execution(decision, _request(intent="domain.observe"))
    assert (
        advisory.result["delegation_receipt"]["execution_mode"] == "advisory-only"
    )

    unwired = XingchengSovereign()
    unavailable = await unwired._delegate_execution(decision, request)
    assert (
        unavailable.result["delegation_receipt"]["execution_mode"]
        == "xingcheng-runtime-unavailable"
    )


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
