"""_base — 主宰共同底座（決策層，無執行權）。"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ..governance.delegation import Delegation
from ..shared.contracts import (
    SovereignOutcome,
    SovereignRequest,
    refusal_outcome,
)
from ..shared.gate import EntryRule, MasterGate

ALL_SOVEREIGN_ROLES: frozenset[str] = frozenset(
    {
        "system-sovereign",
        "permission-sovereign",
        "runtime-sovereign",
        "maintenance-sovereign",
        "resource-sovereign",
        "data-sovereign",
        "integration-sovereign",
        "xingcheng",
        "governance-auditor",
    }
)


class SovereignContext(Protocol):
    router: Any
    permission_gate: Callable[[SovereignRequest], SovereignOutcome]

    def new_delegation(self, requester: str) -> Delegation: ...


class SovereignBase:
    """主宰底座：單一入口 gate、法典引用、唯經委派之執行出口。"""

    sovereign_id: str = ""
    codification: tuple[str, ...] = ()
    required_roles: frozenset[str] = ALL_SOVEREIGN_ROLES

    def __init__(self, context: SovereignContext, rules: tuple[EntryRule, ...] = ()) -> None:
        self._context = context
        self.gate = MasterGate(
            self.sovereign_id,
            required_roles=self.required_roles,
        )
        for entry in rules:
            self.gate.add_entry(entry)

    @property
    def master_entry(self) -> MasterGate:
        return self.gate

    def intents(self) -> tuple[str, ...]:
        return self.gate.intents()

    def delegation(self, requester: str) -> Delegation:
        return self._context.new_delegation(requester)

    def refuse(self, reason_code: str, basis: tuple[str, ...]) -> SovereignOutcome:
        return refusal_outcome(reason_code, basis)

    @staticmethod
    def outcome(accepted: bool, result: dict[str, Any], basis: tuple[str, ...]) -> SovereignOutcome:
        return SovereignOutcome(accepted=accepted, result=result, basis=basis)


__all__ = [
    "ALL_SOVEREIGN_ROLES",
    "SovereignBase",
    "SovereignContext",
]