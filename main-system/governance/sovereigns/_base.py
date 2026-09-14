"""Sovereign Base — 决策层共同底座（无执行权，仅决策/裁决/授权）。

法典依据:
- A12: SOVEREIGN-DECISION: ADJUDICATION-SOURCE:codex; APPLIES:runtime/maintenance/permission-sovereigns; 星澄:outside-decision-chain
- A446: EXECUTION-LAYER-TIERS:dispatch-intake>authorization-and-governance-gate>task-planning>specialized-executor>result-verification>state-event-audit-publication; CONTROL:sub-sovereign; WORK:specialized-module-executor; VERIFY:independent-from-work-step
- A121: BOUNDARY-ENFORCEMENT:governance-gate+audit-ledger+deny-on-violation; MECHANISM:pre-execution-verify+post-execution-audit+violation-stop-record-adjudicate
- A297: TOP-LEVEL-SOVEREIGNS: EXECUTION-POWER:none; SEPARATION:decision actor cannot be execution actor or sole final verifier
- A74: ALL-CODEX-CITATION: enter-through-governance-codex://official
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from governance_rule.execution.codex_reconcile import bounded_lookup
from core_system.codex_decision import (
    DecisionBasis,
    SovereignOutcome,
    SovereignRequest,
    accepted_outcome,
    codex_edicts,
    decision_basis,
    refusal_outcome,
    verified_basis,
)

from ._delegation import (
    attach_delegation_receipt,
    consume_delegation,
    mint_delegation,
    mint_delegation_receipt,
    record_delegation_outcome,
)
from ._requester_verification import (
    _GOVERNED_IN_PROCESS_ACTORS,
    verify_requester as _verify_requester_impl,
)
from ..independent_verifier import IndependentVerifier, VerificationVerdict

# Lazy import to avoid circular dependency
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from governance_rule.execution.authentication import GovernanceAuthenticationService


@dataclass(frozen=True)
class SovereignIdentity:
    """主宰身份（来自法典 sovereigns 表）。"""
    sovereign_id: str
    name: str
    area: str
    rank: str
    basis: str
    duties: tuple[str, ...]
    powers: tuple[str, ...]
    prohibitions: tuple[str, ...]

    @classmethod
    def from_codex(cls, sovereign_id: str) -> "SovereignIdentity":
        # A435 BOUNDED_MACHINE_LOOKUP: identity/status/binding resolution
        # through the official entry, scoped to this one sovereign record.
        s = bounded_lookup(
            sovereign_id,
            purpose="adjudication",
            scope=(f"sovereign:{sovereign_id}",),
            reader=lambda ctx: ctx.sovereign(sovereign_id),
        )
        if s is None:
            raise KeyError(f"Sovereign {sovereign_id!r} not found in Codex")
        return cls(
            sovereign_id=s.id,
            name=s.name,
            area=s.area,
            rank=s.rank,
            basis=s.basis,
            duties=tuple(s.duties),
            powers=tuple(s.powers),
            prohibitions=tuple(s.prohibitions),
        )


def _stamp_delegation(
    request: SovereignRequest,
    sovereign_id: str,
    target_sovereign_id: str,
) -> SovereignRequest:
    """Stamp a single-use delegation nonce onto the forwarded request."""
    payload = dict(request.payload)
    payload["_delegated_by"] = sovereign_id
    payload["_delegation_nonce"] = mint_delegation(
        sovereign_id, target_sovereign_id, request.intent
    )
    return SovereignRequest(
        intent=request.intent,
        subject=request.subject,
        requester=sovereign_id,
        payload=payload,
    )


def _attach_target_receipt(
    outcome: SovereignOutcome,
    request: SovereignRequest,
    sovereign_id: str,
    target_sovereign_id: str,
) -> SovereignOutcome:
    """Attach a verifiable delegation receipt carrying the target's trail."""
    target_receipts: tuple[dict[str, Any], ...] = ()
    target_result = outcome.result or {}
    if isinstance(target_result, dict):
        summary = target_result.get("execution_receipts")
        if isinstance(summary, dict):
            target_receipts = tuple(summary.get("tiers", ()))
    receipt = mint_delegation_receipt(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=outcome.accepted,
        reason_code=outcome.refusal.reason_code if outcome.refusal else "",
        execution_mode="delegated-to-target",
        basis=outcome.basis,
        target_sovereign=target_sovereign_id,
        target_receipts=target_receipts,
    )
    result = dict(outcome.result or {})
    result["delegation_receipt"] = receipt.to_dict()
    return SovereignOutcome(
        accepted=outcome.accepted,
        refusal=outcome.refusal,
        result=result,
        basis=outcome.basis,
    )


class SovereignBase(ABC):
    """主宰底座：单一入口 gate、法典引用、唯经委派之执行出口。"""

    sovereign_id: str = ""

    def __init__(self, app: Any | None = None) -> None:
        self.app = app
        self._identity = SovereignIdentity.from_codex(self.sovereign_id)
        self._started = False
        self._state: dict[str, Any] = {}
        # Child registry — populated by the governed executor at activation
        # (A334: each sub-sovereign is registered under exactly one parent).
        self._sub_sovereigns: dict[str, Any] = {}
        # Per-child consecutive-failure counts, fed by the governed
        # executor and by children reporting through ``report_to_parent``.
        self._child_failure_counts: dict[str, int] = {}
        # A446 independent verifier — never the work step; domain checks may
        # be registered by subclasses via ``register_verification_check``.
        self._independent_verifier = IndependentVerifier()

    @property
    def identity(self) -> SovereignIdentity:
        return self._identity

    @property
    def area(self) -> str:
        return self._identity.area

    @property
    def role(self) -> str:
        return self._identity.sovereign_id

    @property
    def started(self) -> bool:
        return self._started

    # -------------------------------------------------------------------------
    # Codex decision basis
    # -------------------------------------------------------------------------

    def edicts(self) -> list[dict[str, str]]:
        """取得管辖领域的法典敕令（决策依据）。"""
        return codex_edicts(self.area)

    def basis(self) -> dict[str, Any]:
        """取得完整决策基础（法典+主宰子法）。"""
        return decision_basis(self.area)

    def verified_basis(self, *refs: str) -> DecisionBasis:
        """验证并返回决策依据 token。"""
        return verified_basis(refs)

    # -------------------------------------------------------------------------
    # Single entry gate (A63/A64)
    # -------------------------------------------------------------------------

    async def handle(self, request: SovereignRequest) -> SovereignOutcome:
        """单一决策入口：裁决 -> 授权 -> 委派执行（全阶段 A446 receipts）。

        Every request flows through ``SovereignExecutionPipeline`` so all six
        A446 tiers are receipted (dispatch-intake, authorization gate,
        task-planning, specialized-executor, result-verification,
        audit-publication); the executor never self-declares success — the
        independent verifier and the mandatory audit publication decide —
        and any missing receipt fails closed.
        """
        from ..execution_pipeline import SovereignExecutionPipeline

        return await SovereignExecutionPipeline(self).run(request)

    def register_verification_check(self, intent: str, check: Any) -> None:
        """Register an independent domain check for ``intent`` (A446)."""
        self._independent_verifier.register(intent, check)

    def verify_execution_result(
        self, intent: str, executor_actor: str, outcome: SovereignOutcome
    ) -> VerificationVerdict:
        """Independent verification of an executor result (A446/A121)."""
        return self._independent_verifier.verify(intent, executor_actor, outcome)

    async def _verify_requester(self, request: SovereignRequest) -> bool:
        """验证请求者身份（A10/A11/A116/A121/A174 fail-closed）.

        Delegates to ``_requester_verification.verify_requester`` so the
        fail-closed identity-attestation contract lives in one place.
        """
        return _verify_requester_impl(self, request)

    def _authenticate_token_claims(
        self, request: SovereignRequest, token: str
    ) -> Any | None:
        """Verify a capability token against this sovereign's request scope."""
        auth = getattr(self.app, "governance_auth", None) or getattr(
            self.app, "governance", None
        )
        if auth is None:
            return None
        auth_service = getattr(auth, "authentication", None) or getattr(
            auth, "authentication_service", None
        )
        if auth_service is None:
            return None
        try:
            claims = auth_service.authenticate_token(token)
        except (ValueError, KeyError, PermissionError, RuntimeError, ImportError):
            # Expected authentication failures deny (fail-closed); unexpected
            # programming errors must surface instead of being downgraded.
            return None
        # The token must prove the requester identity — ``bound_tool_id``
        # belongs to the *requester's* attestation, never to the target
        # sovereign.
        if claims.actor != request.requester:
            return None
        if claims.capability not in {
            "sovereign.request",
            f"{self.area}.request",
            request.intent,
        }:
            return None
        return claims

    def _claims_sovereign_identity(self, requester: str) -> bool:
        """True when the requester string names a sovereign identity."""
        value = str(requester or "").strip()
        if not value:
            return False
        if value.endswith(("-sovereign", "-sub-sovereign")):
            return True
        from ..registries import parent_of, resolve_sovereign

        if parent_of(value) is not None:
            return True
        return resolve_sovereign(self.app, value) is not None

    def _verify_intent(self, intent: str) -> bool:
        """验证意图是否在管辖敕令范围内。"""
        allowed = {e["id"] for e in self.edicts()}
        return intent in allowed or intent.startswith("governance.")

    # ------------------------------------------------------------------
    # Sub-sovereign registry + A334 parent-authority adjudication
    # ------------------------------------------------------------------

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)

    def authorize_child_activation(self, child_identity: str) -> SovereignOutcome:
        """A334: adjudicate whether this sovereign may dispatch a child start.

        Fail-closed: the child must be registered in this sovereign's
        registry AND the codex ``sovereign_hierarchy_registry`` must declare
        this sovereign as the child's single parent.
        """
        from ..registries import parent_of

        child = self._sub_sovereigns.get(child_identity)
        if child is None:
            return refusal_outcome("CHILD_NOT_REGISTERED", ("A334", "A130"))
        if parent_of(child_identity) != self.sovereign_id:
            return refusal_outcome("NOT_CODEX_PARENT", ("A334",))
        return accepted_outcome(
            {
                "child": child_identity,
                "parent": self.sovereign_id,
                "dispatch": "authorized",
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A334", "A130")),
        )

    # ------------------------------------------------------------------
    # Inter-sovereign coordination (A334 routed delegation)
    # ------------------------------------------------------------------

    def resolve_sovereign(self, sovereign_id: str) -> Any | None:
        """Resolve another sovereign instance via the A334 hierarchy."""
        from ..registries import resolve_sovereign

        return resolve_sovereign(self.app, sovereign_id)

    async def delegate_to(
        self, target_sovereign_id: str, request: SovereignRequest
    ) -> SovereignOutcome:
        """Route a request through the target sovereign's single entry gate.

        The requester is rewritten to this sovereign's identity so the
        target's A10/A11 gates observe the true sovereign origin; a
        sub-sovereign target additionally enforces its A334 single-parent
        check, so only the codex parent can delegate into it.  Fails
        closed when the target is not materialized or not started.

        The returned outcome carries a verifiable ``DelegationReceipt`` in
        ``result["delegation_receipt"]`` with the target's execution
        receipt trail — so callers can verify the delegation actually
        reached the target and was processed, not merely declared.
        """
        target = self.resolve_sovereign(target_sovereign_id)
        if target is None:
            return refusal_outcome("TARGET_SOVEREIGN_UNAVAILABLE", ("A334",))
        if not getattr(target, "started", False):
            return refusal_outcome(
                "TARGET_SOVEREIGN_NOT_STARTED", ("A10", "A11")
            )
        forwarded = _stamp_delegation(
            request, self.sovereign_id, target_sovereign_id
        )
        outcome = await target.handle(forwarded)
        return _attach_target_receipt(
            outcome, request, self.sovereign_id, target_sovereign_id
        )

    # ------------------------------------------------------------------
    # Child failure tracking (shared by all sovereign parents)
    # ------------------------------------------------------------------

    def record_child_failure(self, child_id: str) -> int:
        """Record a consecutive child failure; returns the new count."""
        count = self._child_failure_counts.get(child_id, 0) + 1
        self._child_failure_counts[child_id] = count
        return count

    def record_child_success(self, child_id: str) -> None:
        """Clear the consecutive-failure counter after a child recovers."""
        self._child_failure_counts.pop(child_id, None)

    def child_failure_count(self, child_id: str) -> int:
        return self._child_failure_counts.get(child_id, 0)

    @abstractmethod
    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """核心裁决逻辑（子类实作）。"""
        ...

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """委派执行给受治理执行器（A446/A121）。

        Fail-closed default: a sovereign that does not override this hook
        cannot claim successful execution.  Returning the bare adjudication
        result would mask the absence of execution behind an accepted
        outcome, violating A446 (EXECUTION-LAYER-TIERS requires a real
        specialized-executor step) and A121 (post-execution-audit must
        record an actual execution, not a decision echo).

        Subclasses MUST override this hook to do one of:
          * dispatch the decision to a registered governed executor,
            run independent verification, and record the audit trail; or
          * attest that the adjudication was a pure decision / query with
            no execution side-effect (e.g. permission.query, runtime.status)
            and return the decision unchanged with that attestation recorded.
        """
        return refusal_outcome(
            "EXECUTION_NOT_DELEGATED",
            self.verified_basis("A446", "A121"),
        )

    def _attach_delegation_receipt(
        self,
        decision: SovereignOutcome,
        request: SovereignRequest,
        execution_mode: str = "decision-only",
    ) -> SovereignOutcome:
        """Mint a verifiable delegation receipt and attach it to the outcome.

        Thin wrapper around ``attach_delegation_receipt`` (A446/A121).
        """
        return attach_delegation_receipt(
            decision, request, self.sovereign_id, execution_mode
        )

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """启动主宰（决策层初始化，不执行业务逻辑）。"""
        if self._started:
            return {"role": self.role, "status": "already_started"}

        self._state = {
            "role": self.role,
            "area": self.area,
            "started_at": self._iso_now(),
            "execution_delegation": "governed-executor-only",
        }
        self._started = True
        await self._on_start()
        return self._state

    async def stop(self) -> None:
        """停止主宰。"""
        if not self._started:
            return
        await self._on_stop()
        self._started = False
        self._state = {"stopped_at": self._iso_now()}

    async def _on_start(self) -> None:
        """子类覆写：启动时的额外初始化。"""
        pass

    async def start_supervision(self) -> None:
        """Start observation/supervision loops (A297 separation).

        The sovereign's ``start()`` only initializes the decision layer.
        Supervision/automation loops are started separately by the
        governed executor calling this method after ``start()`` returns,
        so the decision/observe boundary is explicit: the sovereign
        decides, the executor starts the observation work.
        """
        pass

    async def stop_supervision(self) -> None:
        """Stop observation/supervision loops (A297 separation)."""
        pass

    async def _on_stop(self) -> None:
        """子类覆写：停止时的清理。"""
        pass

    def status(self) -> dict[str, Any]:
        """状态回报（唯读）。"""
        return {**self._state, "started": self._started}

    def live_status(self) -> dict[str, Any]:
        """即时状态（供编排层查询）。"""
        return self.status()

    def orchestration_status(self) -> dict[str, Any]:
        """编排层状态（含子系统健康）。"""
        return {
            "state": "active" if self._started else "stopped",
            "owner": self.role,
            "children": {
                child_id: bool(getattr(child, "started", False))
                for child_id, child in self._sub_sovereigns.items()
            },
            "child_failure_counts": dict(self._child_failure_counts),
        }

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["SovereignBase", "SovereignIdentity"]