"""Directory sync manager — cross-sovereign directory synchronization.

負責：
1. 跨主權目錄的同步
2. 目錄完整性驗證
3. 變更檢測與同步
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.governance_policy import governance_policy_snapshot

_logger = logging.getLogger("gptbridge.permission_automation")


class DirectorySyncManager:
    """目錄同步管理器。

    負責：
    1. 跨主權目錄的同步
    2. 目錄完整性驗證
    3. 變更檢測與同步
    """

    def __init__(
        self,
        permission_sovereign: Any,
        sync_interval: float = 300.0,  # 5分鐘
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.sync_interval = sync_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_sync_hash: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="directory-sync-manager")
        _logger.info("DirectorySyncManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("DirectorySyncManager stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._sync_directories()
            except Exception as e:
                _logger.error(f"Directory sync failed: {e}")
            try:
                await asyncio.sleep(self.sync_interval)
            except asyncio.CancelledError:
                break

    async def _sync_directories(self) -> None:
        """同步所有受管目錄。"""
        # 獲取當前目錄快照
        code_rules = code_rule_directory_snapshot()
        authority = directory_authority_snapshot()
        identities = identity_group_snapshot()
        permissions = identity_permission_snapshot()
        capabilities, repairs = capability_boundary_snapshot()
        policy = governance_policy_snapshot()

        # 計算同步雜湊
        sync_data = f"{code_rules.initial_code_version}{authority.authority_version_policy.current_version}{len(identities.identities)}"
        current_hash = hashlib.sha256(sync_data.encode()).hexdigest()

        if self._last_sync_hash is None:
            self._last_sync_hash = current_hash
            return

        if current_hash != self._last_sync_hash:
            _logger.info("Directory changes detected, sync triggered")
            self._last_sync_hash = current_hash
            await self._notify_directory_children(sync_data)

    async def _notify_directory_children(self, sync_data: str) -> None:
        """Notify the permission-sovereign's codex children of the change.

        Directory and identity-group coordination belongs to
        ``directory-sub-sovereign`` / ``identity-group-sub-sovereign``
        (A316/A317) — delivery goes through the sovereign's
        ``delegate_to`` so the child gate sees the real parent as
        requester.  Undelivered notifications are logged, not raised.
        """
        from core_system.codex_decision import SovereignRequest

        for child_id in (
            "directory-sub-sovereign",
            "identity-group-sub-sovereign",
        ):
            try:
                outcome = await self.permission_sovereign.delegate_to(
                    child_id,
                    SovereignRequest(
                        intent="sync",
                        subject="directory-change",
                        requester="permission-automation",
                        payload={
                            "target": child_id,
                            "sync_status": "directory-changed",
                            "hash_source": sync_data[:64],
                        },
                    ),
                )
                if not getattr(outcome, "accepted", False):
                    _logger.warning(
                        "Directory sync notification to %s refused: %s",
                        child_id,
                        getattr(outcome, "refusal", None),
                    )
            except Exception as e:
                _logger.warning(
                    "Directory sync notification to %s failed: %s",
                    child_id,
                    e,
                )

    def get_sync_status(self) -> dict[str, Any]:
        return {
            "last_sync_hash": self._last_sync_hash,
            "sync_interval": self.sync_interval,
        }


__all__ = ["DirectorySyncManager"]
