"""Repair Backup Sync Sub-Sovereign — 修復備份同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: repair-backup-sync-sub-sovereign (position 34)
- area: repair-backup-recovery-synchronization
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A322
"""

from __future__ import annotations

from typing import Any

from core_system.codex_decision import (
    SovereignOutcome,
    SovereignRequest,
    accepted_outcome,
    refusal_outcome,
)

from ._base import SubSovereignBase
from ._sync_journal import SyncJournal


class RepairBackupSyncSubSovereign(SubSovereignBase):
    """修復備份同步子主權：維修備份復原同步（持久化日誌 + 稽核 + 收斂回報）。"""

    sovereign_id = "repair-backup-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._journal = SyncJournal(self.app, self.sovereign_id)
        latest = self._journal.latest()
        self._sync_state: dict[str, Any] = dict(latest) if latest else {}

    def sync_repair_backup(self, repair_info: dict[str, Any]) -> dict[str, Any]:
        """套用一次修復備份同步（持久化 + 稽核發佈）；回傳日誌紀錄。"""
        record = self._journal.append(
            "repair-backup",
            dict(repair_info) if isinstance(repair_info, dict) else {"info": repair_info},
        )
        self._sync_state = record
        return record

    async def _adjudicate_sync(self, request: SovereignRequest) -> SovereignOutcome:
        """A322: 由同步主宰派發的實際同步職責（持久化、稽核、失敗回報）。"""
        info = request.payload.get("info") or request.payload.get("detail") or {}
        if not isinstance(info, dict):
            info = {"info": info}
        try:
            record = self.sync_repair_backup(info)
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
            self.report_to_parent("failure")
            return refusal_outcome("SYNC_FAILED", self.verified_basis("A322"))
        self.report_to_parent("success")
        return accepted_outcome(
            {
                "synced": True,
                "kind": record["kind"],
                "journal_position": record["position"],
                "synced_at": record["synced_at"],
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322"),
        )

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_state"] = self._sync_state
        base["journal_length"] = len(self._journal.records)
        base["journal_path"] = str(self._journal.path)
        return base


__all__ = ["RepairBackupSyncSubSovereign"]
