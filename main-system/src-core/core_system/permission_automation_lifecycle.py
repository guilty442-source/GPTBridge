"""Permission lifecycle manager — A436/A10/A11/A22.

負責：
1. 權限授予的自動續期、過期處理、撤銷
2. 過期前預警通知
3. 定期清理已撤銷/過期的授予
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core_system.permission_automation_types import (
    PermissionGrant,
    PermissionGrantState,
)

_logger = logging.getLogger("gptbridge.permission_automation")


class PermissionLifecycleManager:
    """權限生命週期管理器。

    負責：
    1. 權限授予的自動續期、過期處理、撤銷
    2. 過期前預警通知
    3. 定期清理已撤銷/過期的授予
    """

    def __init__(
        self,
        permission_sovereign: Any,
        check_interval: float = 3600.0,  # 1小時
        expiry_warning_days: int = 7,
        auto_renew_enabled: bool = True,
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.check_interval = check_interval
        self.expiry_warning_days = expiry_warning_days
        self.auto_renew_enabled = auto_renew_enabled

        self._grants: dict[str, PermissionGrant] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # 續期配置
        self._default_ttl = timedelta(days=30)
        self._max_renewals = 10

    async def start(self) -> None:
        """啟動管理器。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="permission-lifecycle-manager")
        _logger.info("PermissionLifecycleManager started")

    async def stop(self) -> None:
        """停止管理器。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("PermissionLifecycleManager stopped")

    def register_grant(
        self,
        grant_id: str,
        actor: str,
        capability: str,
        action: str,
        target: str,
        data_scope: Optional[str] = None,
        ttl: Optional[timedelta] = None,
        auto_renew: bool = True,
    ) -> PermissionGrant:
        """註冊新權限授予。"""
        now = datetime.now(timezone.utc)
        grant = PermissionGrant(
            grant_id=grant_id,
            actor=actor,
            capability=capability,
            action=action,
            target=target,
            data_scope=data_scope,
            issued_at=now,
            expires_at=now + (ttl or self._default_ttl),
            auto_renew=auto_renew,
        )
        self._grants[grant_id] = grant
        _logger.info(f"Registered permission grant: {grant_id} for {actor}/{capability}")
        return grant

    def get_grant(self, grant_id: str) -> Optional[PermissionGrant]:
        return self._grants.get(grant_id)

    def revoke_grant(self, grant_id: str, reason: str = "") -> bool:
        """撤銷權限授予。"""
        grant = self._grants.get(grant_id)
        if grant is None:
            return False
        grant.state = PermissionGrantState.REVOKED
        grant.revoked_at = datetime.now(timezone.utc)
        grant.revoked_reason = reason
        _logger.warning(f"Revoked permission grant: {grant_id}, reason: {reason}")
        return True

    async def run_once(self) -> None:
        """單次授予檢查——供 automation core 外部驅動（§1.1 自動化集中）。"""
        await self._check_grants()

    async def _run_loop(self) -> None:
        """主循環。"""
        while self._running:
            try:
                await self._check_grants()
            except Exception as e:
                _logger.error(f"PermissionLifecycleManager check failed: {e}")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _check_grants(self) -> None:
        """檢查所有授予狀態。"""
        now = datetime.now(timezone.utc)
        async with self._lock:
            for grant_id, grant in list(self._grants.items()):
                # 檢查過期
                if grant.is_expired() and grant.state == PermissionGrantState.ACTIVE:
                    if self.auto_renew_enabled and grant.auto_renew and grant.renewal_count < self._max_renewals:
                        await self._renew_grant(grant)
                    else:
                        grant.state = PermissionGrantState.EXPIRED
                        _logger.warning(f"Permission grant expired: {grant_id}")

                # 檢查即將過期
                elif grant.is_expiring_soon(self.expiry_warning_days) and grant.state == PermissionGrantState.ACTIVE:
                    grant.state = PermissionGrantState.EXPIRING
                    _logger.warning(f"Permission grant expiring soon: {grant_id}, expires at {grant.expires_at}")

                # 更新檢查時間
                grant.last_checked = now

                # 清理已終結（撤銷/過期）超過30天的授予 — EXPIRED 授予
                # 沒有 revoked_at，要用 expires_at 起算否則永不清除
                if grant.state in (PermissionGrantState.REVOKED, PermissionGrantState.EXPIRED):
                    terminal_at = grant.revoked_at or grant.expires_at
                    if terminal_at and (now - terminal_at) > timedelta(days=30):
                        del self._grants[grant_id]
                        _logger.info(f"Cleaned up old grant: {grant_id}")

    async def _renew_grant(self, grant: PermissionGrant) -> bool:
        """自動續期權限授予。

        續期是權限事務 — 委派給權限主宰的 ``permission.renew`` 裁決
        （A10/A11 完整閘門 + A319 星澄審查），不自作決定。裁決被拒
        或主宰不可用時 fail-closed 標記過期。
        """
        try:
            from core_system.codex_decision import SovereignRequest

            outcome = await self.permission_sovereign.handle(
                SovereignRequest(
                    intent="permission.renew",
                    subject="permission-grant",
                    requester="permission-automation",
                    payload={
                        "grant_id": grant.grant_id,
                        "actor": grant.actor,
                        "capability": grant.capability,
                        "action": grant.action,
                        "target": grant.target,
                        "data_scope": grant.data_scope,
                        "renewal_count": grant.renewal_count,
                    },
                )
            )
            if not getattr(outcome, "accepted", False):
                _logger.warning(
                    "Renewal adjudication refused for %s: %s",
                    grant.grant_id,
                    getattr(outcome, "refusal", None),
                )
                grant.state = PermissionGrantState.EXPIRED
                return False
            grant.expires_at = datetime.now(timezone.utc) + self._default_ttl
            grant.renewed_at = datetime.now(timezone.utc)
            grant.renewal_count += 1
            grant.state = PermissionGrantState.ACTIVE
            _logger.info(f"Auto-renewed permission grant: {grant.grant_id}, count: {grant.renewal_count}")
            return True
        except Exception as e:
            _logger.error(f"Failed to renew grant {grant.grant_id}: {e}")
            grant.state = PermissionGrantState.EXPIRED
            return False

    def get_stats(self) -> dict[str, Any]:
        """獲取統計信息。"""
        states = defaultdict(int)
        for grant in self._grants.values():
            states[grant.state.value] += 1
        return {
            "total_grants": len(self._grants),
            "by_state": dict(states),
            "auto_renew_enabled": self.auto_renew_enabled,
            "max_renewals": self._max_renewals,
        }


__all__ = ["PermissionLifecycleManager"]
