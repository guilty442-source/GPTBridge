"""runtime sovereign — 運行主宰。

法典依據：A28 / E7。
  * 負責運行與服務維持：進程存續與運行期完整性；
  * 以本法典之執行委派原則為決策依據（A28）；
  * 實際進程啟停/探測委派受治理執行器（A5/E2）。
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


class RuntimeSovereign(SovereignBase):
    sovereign_id = "runtime"
    codification = ("A28", "E7", "A5", "A12")
    required_roles = frozenset(
        {
            "runtime-sovereign",
            "system-sovereign",
            "maintenance-sovereign",
            "governance-auditor",
        }
    )

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="maintain-process",
                    boundary="process-survival",
                    handler=self._handle_maintain_process,
                    basis=("A28", "E7"),
                ),
                EntryRule(
                    intent="integrity-report",
                    boundary="runtime-integrity",
                    handler=self._handle_integrity_report,
                    basis=("A28",),
                ),
                EntryRule(
                    intent="status",
                    boundary="runtime-status",
                    handler=self._handle_status,
                    basis=("A28", "A12"),
                ),
            ),
        )

    def _handle_maintain_process(self, request: SovereignRequest) -> SovereignOutcome:
        action = str(request.payload.get("action") or "")
        process = str(request.payload.get("process") or "")
        delegation = self.delegation(self.sovereign_id)
        if action == "probe":
            outcome = delegation.delegate(
                "probe-process", {"target": request.payload.get("target") or ""}
            )
        elif action == "spawn":
            outcome = delegation.delegate(
                "spawn-managed-process", {"name": process}
            )
        elif action == "stop":
            outcome = delegation.delegate(
                "stop-managed-process", {"target": request.payload.get("target") or ""}
            )
        else:
            return refusal_outcome("UNKNOWN_PROCESS_ACTION", ("A10",))
        if not outcome.accepted:
            return refusal_outcome(
                outcome.refusal.reason_code if outcome.refusal else "EXECUTOR_FAILED",
                ("A28", "A11"),
            )
        return accepted_outcome(outcome.result, ("A28", "A5", "E2"))

    def _handle_integrity_report(self, request: SovereignRequest) -> SovereignOutcome:
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            "runtime-integrity", {"guard": True}
        )
        return accepted_outcome(
            {**outcome.result, "authority": "runtime-sovereign"},
            ("A28",),
        )

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A28", "A12"),
        )


__all__ = ["RuntimeSovereign"]