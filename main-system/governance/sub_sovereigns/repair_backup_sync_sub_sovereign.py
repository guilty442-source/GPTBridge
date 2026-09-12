"""Repair Backup Sync Sub-Sovereign — 修復備份同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: repair-backup-sync-sub-sovereign (position 34)
- area: repair-backup-recovery-synchronization
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A322
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class RepairBackupSyncSubSovereign(SubSovereignBase):
    """修復備份同步子主權：維修備份復原同步。"""

    sovereign_id = "repair-backup-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._sync_state: dict[str, Any] = {}

    def sync_repair_backup(self, repair_info: dict[str, Any]) -> None:
        """同步修復備份狀態。"""
        self._sync_state = {
            "repair_backup": repair_info,
            "synced_at": self._iso_now(),
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_state"] = self._sync_state
        return base


__all__ = ["RepairBackupSyncSubSovereign"]