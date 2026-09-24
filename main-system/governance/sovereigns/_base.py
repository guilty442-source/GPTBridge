"""Sovereign Base — 决策层共同底座（无执行权，仅决策/裁决/授权）。

法典依据:
- A12: SOVEREIGN-DECISION: ADJUDICATION-SOURCE:codex; APPLIES:runtime/maintenance/permission-sovereigns; 星澄:outside-decision-chain
- A446: EXECUTION-LAYER-TIERS:dispatch-intake>authorization-and-governance-gate>task-planning>specialized-executor>result-verification>state-event-audit-publication; CONTROL:sub-sovereign; WORK:specialized-module-executor; VERIFY:independent-from-work-step
- A121: BOUNDARY-ENFORCEMENT:governance-gate+audit-ledger+deny-on-violation; MECHANISM:pre-execution-verify+post-execution-audit+violation-stop-record-adjudicate
- A297: TOP-LEVEL-SOVEREIGNS: EXECUTION-POWER:none; SEPARATION:decision actor cannot be execution actor or sole final verifier
- A74: ALL-CODEX-CITATION: enter-through-governance-codex://official
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
from .mixins import (
    AuthBase,
    CodexBase,
    DelegationBase,
    ExecutionBase,
    FailureTrackingBase,
    LifecycleBase,
    StatusBase,
    VerificationBase,
)
from ..independent_verifier import IndependentVerifier, VerificationVerdict


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


class SovereignBase(
    CodexBase,
    AuthBase,
    DelegationBase,
    FailureTrackingBase,
    ExecutionBase,
    LifecycleBase,
    StatusBase,
    VerificationBase,
    ABC,
):
    """主宰底座：单一入口 gate、法典引用、唯经委派之执行出口（A446/A121）。"""

    sovereign_id: str = ""

    def __init__(self, app: Any | None = None) -> None:
        self._app = app
        self._identity = SovereignIdentity.from_codex(self.sovereign_id)
        # Mixin initialization is handled by cooperative __init__ via super()
        # Ensure mixin state is initialized
        self._sub_sovereigns = getattr(self, '_sub_sovereigns', {})
        self._child_failure_counts = getattr(self, '_child_failure_counts', {})
        self._state = getattr(self, '_state', {})
        self._started = getattr(self, '_started', False)
        self._independent_verifier = getattr(self, '_independent_verifier', IndependentVerifier())

    @property
    def app(self) -> Any:
        return self._app

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

    @abstractmethod
    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """核心裁决逻辑（子类实作）。"""
        ...


__all__ = ["SovereignBase", "SovereignIdentity"]