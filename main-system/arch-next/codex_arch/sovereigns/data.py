"""data sovereign — 資料主宰。

法典依據：P14 / A31 / A33 / E18 / E20。
  * 負責一切資料本體事務：結構化資料、語意索引與版歷史之
    存取規範、一致性、完整性查核執行與資料目錄（A31）；
  * 資料完整性查核執行歸本主宰（A33），健康呈現歸維護主宰；
  * 禁止越權管理資源或權限（A31 prohibition）；本身無執行權（E18）。
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

_INTEGRITY_KIND_TO_EXECUTOR: dict[str, str] = {
    "sql": "sql-integrity",
    "semantic-index": "semantic-index-health",
    "version-history": "version-history-health",
}


class DataSovereign(SovereignBase):
    sovereign_id = "data"
    codification = ("P14", "A31", "A33", "E18", "E20", "A12")
    required_roles = frozenset(
        {
            "data-sovereign",
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
                    intent="integrity-health",
                    boundary="data-integrity-check",
                    handler=self._handle_integrity_health,
                    basis=("A31", "A33"),
                ),
                EntryRule(
                    intent="data-directory",
                    boundary="data-directory",
                    handler=self._handle_data_directory,
                    basis=("A31",),
                ),
                EntryRule(
                    intent="access-spec",
                    boundary="data-access-spec",
                    handler=self._handle_access_spec,
                    basis=("A31",),
                ),
                EntryRule(
                    intent="status",
                    boundary="data-status",
                    handler=self._handle_status,
                    basis=("A31", "A12"),
                ),
            ),
        )

    def _handle_integrity_health(self, request: SovereignRequest) -> SovereignOutcome:
        kind = str(request.payload.get("kind") or "sql").strip()
        if kind not in _INTEGRITY_KIND_TO_EXECUTOR:
            kind = "sql"
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate(
            _INTEGRITY_KIND_TO_EXECUTOR[kind],
            {"target": request.payload.get("target") or ""},
        )
        if not outcome.accepted:
            return refusal_outcome(
                outcome.refusal.reason_code if outcome.refusal else "DATA_INTEGRITY_FAILED",
                ("A31", "A11"),
            )
        return accepted_outcome(
            {"kind": kind, "health": outcome.result}, ("A31", "A33")
        )

    def _handle_data_directory(self, request: SovereignRequest) -> SovereignOutcome:
        delegation = self.delegation(self.sovereign_id)
        outcome = delegation.delegate("data-directory", {})
        if not outcome.accepted:
            return refusal_outcome("DATA_DIRECTORY_UNAVAILABLE", ("A31", "A11"))
        return accepted_outcome(outcome.result, ("A31",))

    def _handle_access_spec(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "access_mode": "deny-by-default-explicit-allow",
                "owners": [self.sovereign_id],
                "basis": list(self.codification),
            },
            ("A31", "A10"),
        )

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
            },
            ("A31", "A12"),
        )


__all__ = ["DataSovereign"]