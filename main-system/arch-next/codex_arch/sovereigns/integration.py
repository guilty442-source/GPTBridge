"""integration sovereign — 整合主宰。

法典依據：P15 / A32 / A34 / E19 / E20。
  * 負責跨主宰與跨模組之結構性介面、通道、同步與匯流（A32）；
  * 禁止涉入決策層協調（A32/E19）；決策協調歸星澄（A34）；
  * 本身無執行權（E19）；通道/同步實作委派受治理執行器。
"""

from __future__ import annotations

from ..shared.contracts import (
    SovereignOutcome,
    SovereignRequest,
    accepted_outcome,
    refusal_outcome,
)
from ..shared.gate import EntryRule
from ._base import SovereignBase


class IntegrationSovereign(SovereignBase):
    sovereign_id = "integration"
    codification = ("P15", "A32", "A34", "E19", "E20", "A12")
    required_roles = frozenset(
        {
            "integration-sovereign",
            "system-sovereign",
            "governance-auditor",
        }
    )

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="channel",
                    boundary="structural-channel",
                    handler=self._handle_channel,
                    basis=("A32", "E19"),
                ),
                EntryRule(
                    intent="bus",
                    boundary="sync-bus",
                    handler=self._handle_bus,
                    basis=("A32", "E19"),
                ),
                EntryRule(
                    intent="interface-sync",
                    boundary="structural-interface",
                    handler=self._handle_interface_sync,
                    basis=("A32",),
                ),
                EntryRule(
                    intent="coordinate-decision",
                    boundary="decision-layer-coordination",
                    handler=self._handle_coordinate_decision,
                    basis=("A32", "A34", "E19"),
                ),
                EntryRule(
                    intent="status",
                    boundary="integration-status",
                    handler=self._handle_status,
                    basis=("A32", "A12"),
                ),
            ),
        )

    def _handle_channel(self, request: SovereignRequest) -> SovereignOutcome:
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            "channel-register",
            {
                "channel": str(request.subject or ""),
                "kind": str(request.payload.get("kind") or ""),
            },
        )
        if not outcome.accepted:
            return refusal_outcome("CHANNEL_FAILED", ("A32", "A11"))
        return accepted_outcome(outcome.result, ("A32", "E19"))

    def _handle_bus(self, request: SovereignRequest) -> SovereignOutcome:
        action = str(request.payload.get("action") or "")
        delegation = self.delegation(self.sovereign_id)
        if action == "publish":
            outcome = delegation.delegate(
                "bus-publish", {"event": str(request.subject or "")}
            )
        elif action == "subscribe":
            outcome = delegation.delegate("bus-subscribe", {"event": str(request.subject or "")})
        else:
            return refusal_outcome("UNKNOWN_BUS_ACTION", ("A10",))
        if not outcome.accepted:
            return refusal_outcome("BUS_FAILED", ("A32", "A11"))
        return accepted_outcome(outcome.result, ("A32", "E19"))

    def _handle_interface_sync(self, request: SovereignRequest) -> SovereignOutcome:
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            "iface-sync", {"interface": str(request.subject or "")}
        )
        if not outcome.accepted:
            return refusal_outcome("SYNC_FAILED", ("A32", "A11"))
        return accepted_outcome(outcome.result, ("A32",))

    def _handle_coordinate_decision(self, request: SovereignRequest) -> SovereignOutcome:
        return refusal_outcome(
            "DECISION_COORDINATION_NOT_OWNED", ("A32", "A34", "E19")
        )

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A32", "A12"),
        )


__all__ = ["IntegrationSovereign"]