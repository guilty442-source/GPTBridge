"""maintenance sovereign — 系統維護主宰。

法典依據：A24 / A25 / A33 / E8 / E20。
  * 職責：更新、系統健康監控（含資料完整性呈現）、自動修復、故障判定、備份；
  * 決策依本法典與治理授權（A24）；
  * 資料完整性查核執行歸資料主宰，本主宰僅呈現其健康狀態（A33）；
  * 系統健康監控不得忽略或掩蓋資料完整性異常（A25）。
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


class MaintenanceSovereign(SovereignBase):
    sovereign_id = "maintenance"
    codification = ("A24", "A25", "A33", "E8", "E20", "A12")
    required_roles = frozenset(
        {
            "maintenance-sovereign",
            "system-sovereign",
            "governance-auditor",
        }
    )

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="health-report",
                    boundary="system-health-monitor",
                    handler=self._handle_health_report,
                    basis=("A24", "A25", "A33"),
                ),
                EntryRule(
                    intent="fault-diagnosis",
                    boundary="fault-determination",
                    handler=self._handle_fault_diagnosis,
                    basis=("A24", "E8"),
                ),
                EntryRule(
                    intent="apply-repair",
                    boundary="automatic-repair",
                    handler=self._handle_apply_repair,
                    basis=("A24", "E8", "A11"),
                ),
                EntryRule(
                    intent="status",
                    boundary="maintenance-status",
                    handler=self._handle_status,
                    basis=("A24", "A12"),
                ),
            ),
        )

    def _handle_health_report(self, request: SovereignRequest) -> SovereignOutcome:
        router = self._context.router
        data_health = router.route(
            "data",
            SovereignRequest(
                intent="integrity-health",
                subject="health",
                requester=request.requester,
            ),
        )
        runtime_status = router.route(
            "runtime",
            SovereignRequest(
                intent="status",
                subject="runtime",
                requester=request.requester,
            ),
        )
        if not data_health.accepted:
            return refusal_outcome(
                "DATA_INTEGRITY_UNAVAILABLE", ("A25", "A33")
            )
        delegation = self.delegation(self.sovereign_id)
        aggregated = delegation.delegate(
            "health-aggregate",
            {
                "data_integrity": data_health.result,
                "runtime": runtime_status.result if runtime_status.accepted else {},
            },
        )
        if not aggregated.accepted:
            return refusal_outcome("HEALTH_AGGREGATE_FAILED", ("A24",))
        return accepted_outcome(
            {**aggregated.result, "presented_by": self.sovereign_id},
            ("A24", "A25", "A33"),
        )

    def _handle_fault_diagnosis(self, request: SovereignRequest) -> SovereignOutcome:
        failure_code = str(request.payload.get("failure_code") or "").strip()
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate("fault-plans", {"failure_code": failure_code})
        if not outcome.accepted:
            return refusal_outcome("FAULT_PLAN_UNAVAILABLE", ("A24", "A11"))
        return accepted_outcome(outcome.result, ("A24", "E8"))

    def _handle_apply_repair(self, request: SovereignRequest) -> SovereignOutcome:
        failure_code = str(request.payload.get("failure_code") or "").strip().upper()
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            "source-selfrepair", {"failure_code": failure_code}
        )
        if not outcome.accepted:
            return refusal_outcome(
                outcome.refusal.reason_code if outcome.refusal else "REPAIR_UNRESOLVED",
                ("A24", "A11"),
            )
        return accepted_outcome(outcome.result, ("A24", "A11"))

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A24", "A12"),
        )


__all__ = ["MaintenanceSovereign"]