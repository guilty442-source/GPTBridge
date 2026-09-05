"""resource sub-sovereign — 資源子主宰（收編於系統主宰之下）。

法典依據：P11 / A26 / P13 / A30 / E17。
  * 負責一切資源本體事務：記憶體、磁碟、模型與運算資源之
    狀態監控、配置與委派釋放（A30）；
  * 已收編為系統主宰之子主宰（system-resource-sub-sovereign）；
  * 本身無執行權（E17）；狀態量測/配置/釋放委派受治理執行器；
  * 禁止越權執行或越權管理資料或權限（A30 prohibition）。
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

_RESOURCE_KIND_TO_EXECUTOR: dict[str, str] = {
    "memory": "memory-state",
    "disk": "disk-state",
    "compute": "compute-state",
    "model": "model-state",
}


class ResourceSubSovereign(SovereignBase):
    sovereign_id = "resource"
    codification = ("P13", "A30", "E17", "A5", "A12")
    required_roles = frozenset(
        {
            "system-resource-sub-sovereign",
            "system-sovereign",
            "governance-auditor",
        }
    )

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="resource-state",
                    boundary="resource-state-monitor",
                    handler=self._handle_resource_state,
                    basis=("A30", "E17"),
                ),
                EntryRule(
                    intent="provision-release",
                    boundary="resource-provision-release",
                    handler=self._handle_provision_release,
                    basis=("A30",),
                ),
                EntryRule(
                    intent="status",
                    boundary="resource-status",
                    handler=self._handle_status,
                    basis=("A30", "A12"),
                ),
            ),
        )

    def _handle_resource_state(self, request: SovereignRequest) -> SovereignOutcome:
        kind = str(request.payload.get("kind") or "").strip()
        if kind not in _RESOURCE_KIND_TO_EXECUTOR:
            return refusal_outcome("UNKNOWN_RESOURCE_KIND", ("A10",))
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(_RESOURCE_KIND_TO_EXECUTOR[kind], {})
        if not outcome.accepted:
            return refusal_outcome(
                outcome.refusal.reason_code if outcome.refusal else "RESOURCE_PROBE_FAILED",
                ("A30", "A11"),
            )
        return accepted_outcome(
            {"kind": kind, **outcome.result}, ("A30", "E17")
        )

    def _handle_provision_release(self, request: SovereignRequest) -> SovereignOutcome:
        action = str(request.payload.get("action") or "")
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            "provision-release", {"action": action, "plan": dict(request.payload)}
        )
        if not outcome.accepted:
            return refusal_outcome(
                outcome.refusal.reason_code if outcome.refusal else "PROVISION_FAILED",
                ("A30", "A11"),
            )
        return accepted_outcome(outcome.result, ("A30", "E17"))

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A30", "A12"),
        )


__all__ = ["ResourceSubSovereign"]