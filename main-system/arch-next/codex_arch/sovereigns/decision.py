"""decision sovereign — 系統主宰（頂層總裁主宰）。

法典依據：P11 / A26 / A27 / E15。
  * 負責平台全生命周期編排與依賴整合；
  * 不代決子主宰之運作細節（A27）：只請求各子主宰執行其職責；
  * 本身不執行重權限工作（A5），一切實際執行委派受治理執行器；
  * 決策一律引用法典（P8/A12）。

對子主宰之協調僅屬編排（decision-layer orchestration），其細節由
各子主宰自主決定；本主宰亦不直接持有執行權。
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

ORDINAL_SUB_SOVEREIGNS: tuple[str, ...] = (
    "permission",
    "runtime",
    "maintenance",
    "resource",
    "data",
    "integration",
)


class DecisionSovereign(SovereignBase):
    sovereign_id = "system"
    codification = ("P11", "A26", "A27", "E15", "A5", "A12")
    required_roles = frozenset({"system-sovereign", "governance-auditor"})

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="orchestrate-lifecycle",
                    boundary="full-lifecycle-orchestration",
                    handler=self._handle_orchestrate,
                    basis=("A26", "A27", "E15"),
                ),
                EntryRule(
                    intent="status",
                    boundary="platform-status",
                    handler=self._handle_status,
                    basis=("A26", "A12"),
                ),
            ),
        )

    def _handle_orchestrate(self, request: SovereignRequest) -> SovereignOutcome:
        router = self._context.router
        stages: list[dict[str, object]] = []
        phase_order = list(ORDINAL_SUB_SOVEREIGNS)
        if request.payload.get("phase") in phase_order:
            phase_order = [str(request.payload["phase"])]
        for sovereign_id in phase_order:
            outcome = router.route(
                sovereign_id,
                SovereignRequest(
                    intent="status",
                    subject=sovereign_id,
                    requester=request.requester,
                ),
            )
            stages.append(
                {
                    "sovereign": sovereign_id,
                    "accepted": outcome.accepted,
                    "basis": list(outcome.basis),
                }
            )
        delegation = self.delegation(self.sovereign_id)
        milestone = delegation.delegate(
            "lifecycle-milestone",
            {"structure": {"lifecycle": True, "stages": len(stages)}},
        )
        return accepted_outcome(
            {
                "orchestrated": stages,
                "delegated": milestone.accepted,
                "executor": "lifecycle-milestone",
            },
            ("A26", "A27", "E15"),
        )

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A26", "A12"),
        )


__all__ = ["ORDINAL_SUB_SOVEREIGNS", "DecisionSovereign"]