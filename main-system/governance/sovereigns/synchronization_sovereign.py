"""Synchronization Sovereign — 同步主宰（專門決策主宰，A330 認證更新執行例外）。

法典依據:
- sovereign_id: synchronization-sovereign (position 18)
- area: synchronization-decision
- rank: specialized-decision-sovereign-with-A330-certified-update-execution-exception
- basis: A301
- duties: resource-sync|channel-sync|release-sync|learning-sync|runtime-sync|repair-sync|cleanup-sync|log-sync
- powers: adjudicate-sync-decisions|A330-certified-update-execution
- prohibitions: FORBID:general-execution (except A330)
"""

from __future__ import annotations

from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SynchronizationSovereign(SovereignBase):
    """同步主宰：專門決策，協調各類同步子主宰，A330例外執行。"""

    sovereign_id = "synchronization-sovereign"

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._sync_sub_sovereigns: dict[str, Any] = {}

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：同步決策、A330認證更新、子主宰協調。"""
        intent = request.intent

        if intent == "sync.resource-dependency":
            return await self._adjudicate_resource_sync(request)
        if intent == "sync.channel-contract":
            return await self._adjudicate_channel_sync(request)
        if intent == "sync.release-update":
            return await self._adjudicate_release_sync(request)
        if intent == "sync.learning-evidence":
            return await self._adjudicate_learning_sync(request)
        if intent == "sync.runtime-state":
            return await self._adjudicate_runtime_sync(request)
        if intent == "sync.repair-backup":
            return await self._adjudicate_repair_sync(request)
        if intent == "sync.cleanup-retention":
            return await self._adjudicate_cleanup_sync(request)
        if intent == "sync.automatic-log":
            return await self._adjudicate_log_sync(request)
        if intent == "A330.certified-update":
            return await self._adjudicate_A330_certified_update(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A301", "A322"))

    async def _adjudicate_resource_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 資源分配同步。"""
        return accepted_outcome(
            {
                "sync_type": "resource-dependency",
                "delegated_to": "resource-dependency-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_channel_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 通道契約同步。"""
        return accepted_outcome(
            {
                "sync_type": "channel-contract",
                "delegated_to": "channel-contract-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_release_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 發布更新同步。"""
        return accepted_outcome(
            {
                "sync_type": "release-update",
                "delegated_to": "release-update-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_learning_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 學習證據同步。"""
        return accepted_outcome(
            {
                "sync_type": "learning-evidence",
                "delegated_to": "learning-evidence-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_runtime_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 運行狀態同步。"""
        return accepted_outcome(
            {
                "sync_type": "runtime-state-checkpoint",
                "delegated_to": "runtime-state-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_repair_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 維修備份同步。"""
        return accepted_outcome(
            {
                "sync_type": "repair-backup-recovery",
                "delegated_to": "repair-backup-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_cleanup_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 清理保留同步。"""
        return accepted_outcome(
            {
                "sync_type": "cleanup-retention",
                "delegated_to": "cleanup-retention-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_log_sync(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: 自動日誌審計同步。"""
        return accepted_outcome(
            {
                "sync_type": "automatic-log-audit",
                "delegated_to": "automatic-log-sync-sub-sovereign",
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301"),
        )

    async def _adjudicate_A330_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A330: 認證更新執行例外（唯一執行權限）。"""
        update_type = request.payload.get("update_type")
        if update_type not in {"codex", "governance-policy", "directory"}:
            return refusal_outcome("INVALID_A330_UPDATE_TYPE", self.verified_basis("A330"))

        return accepted_outcome(
            {
                "execution_authorized": True,
                "update_type": update_type,
                "exception": "A330-certified-update-execution",
                "verification": "integrity+identity+history+complete-version-seal",
            },
            self.verified_basis("A330", "A87", "A88"),
        )

    def register_sync_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sync_sub_sovereigns[name] = sovereign

    def get_sync_sub_sovereign(self, name: str) -> Any | None:
        return self._sync_sub_sovereigns.get(name)

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_sub_sovereigns"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._sync_sub_sovereigns.items()
        }
        return base


__all__ = ["SynchronizationSovereign"]