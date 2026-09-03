"""permission sovereign — 權限主宰。

法典依據：P4 / A6 / A7 / A22 / A23 / E4。
  * 凡有關權限之事務一律由權限主宰負責；本身無執行權（A6）；
  * 只依本法典行使權限管理、發放、終止與監管（A6/A22）；
  * 目錄驅動，明示準予清單，無明示即拒絶（A7/A10）；
  * 各模組權限 ID 一律由權限主宰管理（A23）。

權限存簿之寫入委派受治理執行器（permission-ledger）；本主宰本身
不做任何實際持久化或執行。
"""

from __future__ import annotations

from typing import Any

from ..shared.contracts import (
    SovereignOutcome,
    SovereignRequest,
    accepted_outcome,
    refusal_outcome,
)
from ..shared.gate import EntryRule
from ._base import ALL_SOVEREIGN_ROLES, SovereignBase


class PermissionSovereign(SovereignBase):
    """單一授權主入口：全繫權限事務之唯一決策者。"""

    sovereign_id = "permission"
    codification = ("P4", "A6", "A7", "A22", "A23", "E4")
    required_roles = ALL_SOVEREIGN_ROLES

    def __init__(self, context) -> None:
        super().__init__(
            context,
            rules=(
                EntryRule(
                    intent="evaluate",
                    boundary="permission-evaluation",
                    handler=self._handle_evaluate,
                    basis=("A6", "A7", "A10"),
                ),
                EntryRule(
                    intent="issue",
                    boundary="permission-issue",
                    handler=self._handle_issue,
                    basis=("A6", "A23"),
                ),
                EntryRule(
                    intent="terminate",
                    boundary="permission-terminate",
                    handler=self._handle_terminate,
                    basis=("A6", "A22"),
                ),
                EntryRule(
                    intent="supervise",
                    boundary="permission-supervise",
                    handler=self._handle_supervise,
                    basis=("A6", "A7"),
                ),
                EntryRule(
                    intent="status",
                    boundary="permission-status",
                    handler=self._handle_status,
                    basis=("A6", "A12"),
                ),
            ),
        )
        self._directory = context.permission_directory
        self._issued: dict[str, dict[str, Any]] = {}

    def _handle_evaluate(self, request: SovereignRequest) -> SovereignOutcome:
        target = str(request.payload.get("target") or request.subject)
        allowed = self._directory.allows(request.requester, target)
        if not allowed:
            return refusal_outcome("NOT_GRANTED", ("A7", "A10"))
        return accepted_outcome(
            {"target": target, "granted": True}, ("A7", "A10")
        )

    def _handle_issue(self, request: SovereignRequest) -> SovereignOutcome:
        permission_id = str(request.subject or "").strip()
        if not permission_id or ":" not in permission_id:
            return refusal_outcome("INVALID_PERMISSION_ID", ("A23",))
        target = str(request.payload.get("target") or "")
        if not self._directory.allows(request.requester, target):
            return refusal_outcome("NOT_GRANTED", ("A7", "A10"))
        delegation = self.delegation(self.sovereign_id)
        ledger = delegation.delegate(
            "permission-ledger-append",
            {"operation": "issue", "permission_id": permission_id, "target": target},
        )
        if not ledger.accepted:
            return refusal_outcome(
                "LEDGER_FAILED", ("A23", "A11")
            )
        self._issued[permission_id] = {"target": target, "status": "active"}
        return accepted_outcome(
            {"permission_id": permission_id, "target": target, "status": "active"},
            ("A6", "A22", "A23"),
        )

    def _handle_terminate(self, request: SovereignRequest) -> SovereignOutcome:
        permission_id = str(request.subject or "").strip()
        if permission_id not in self._issued:
            return refusal_outcome("UNKNOWN_PERMISSION_ID", ("A22",))
        delegation = self.delegation(self.sovereign_id)
        ledger = delegation.delegate(
            "permission-ledger-append",
            {"operation": "terminate", "permission_id": permission_id},
        )
        if not ledger.accepted:
            return refusal_outcome("LEDGER_FAILED", ("A23", "A11"))
        self._issued[permission_id]["status"] = "terminated"
        return accepted_outcome(
            {"permission_id": permission_id, "status": "terminated"},
            ("A22", "A23"),
        )

    def _handle_supervise(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "issued": len(self._issued),
                "active": sum(
                    1 for record in self._issued.values()
                    if record["status"] == "active"
                ),
                "directory_size": len(self._directory.entries),
            },
            ("A6", "A7"),
        )

    def _handle_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "sovereign": self.sovereign_id,
                "intents": list(self.intents()),
                "codification": list(self.codification),
                "directory_roles": sorted({e.role for e in self._directory.entries}),
            },
            ("A6", "A12"),
        )


__all__ = ["PermissionSovereign"]