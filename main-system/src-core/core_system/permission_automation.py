"""Automated Permission Lifecycle Manager — facade.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.permission_automation_types` — types, enums, dataclasses.
  * :mod:`core_system.permission_automation_lifecycle` — PermissionLifecycleManager.
  * :mod:`core_system.permission_automation_directory` — DirectorySyncManager.
  * :mod:`core_system.permission_automation_compliance` — ComplianceMonitor.
  * :mod:`core_system.permission_automation_audit` — AuditScheduler.
  * :mod:`core_system.permission_automation_healing` — SelfHealingManager.
  * :mod:`core_system.permission_automation_identity` — IdentityGroupManager.

The :class:`PermissionAutomationOrchestrator` remains here as the
unified entry point.

法典依據:
- A436: permission-sovereign supervises execution compliance
- A10/A11: explicit allowlist, fail-closed
- A22: permission termination authority
- A39: actor identity verification
- E4: OWNER:permission-sovereign; ACTIONS:manage-issue-terminate-supervise
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from core_system.permission_automation_audit import AuditScheduler
from core_system.permission_automation_compliance import ComplianceMonitor
from core_system.permission_automation_directory import DirectorySyncManager
from core_system.permission_automation_healing import SelfHealingManager
from core_system.permission_automation_identity import IdentityGroupManager
from core_system.permission_automation_lifecycle import PermissionLifecycleManager
from core_system.permission_automation_types import (
    AuditSchedule,
    ComplianceSeverity,
    ComplianceViolation,
    PermissionGrant,
    PermissionGrantState,
)

_logger = logging.getLogger("gptbridge.permission_automation")


class PermissionAutomationOrchestrator:
    """權限自動化協調器。

    統一管理所有自動化組件：
    - 權限生命週期管理
    - 目錄同步
    - 合規監控
    - 審計排程
    - 自我修復
    - 身份群組管理
    """

    def __init__(
        self,
        permission_sovereign: Any,
        project_root: Optional[Path] = None,
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.project_root = project_root or Path(__file__).resolve().parents[3]

        # 初始化所有子組件
        self.lifecycle = PermissionLifecycleManager(permission_sovereign)
        self.directory_sync = DirectorySyncManager(permission_sovereign)
        self.compliance = ComplianceMonitor(permission_sovereign)
        self.audit = AuditScheduler(Path(__file__).resolve().parents[3])
        self.healing = SelfHealingManager(permission_sovereign)
        self.identity = IdentityGroupManager(permission_sovereign)

        self._running = False
        self._components: list[Any] = [
            self.lifecycle,
            self.directory_sync,
            self.compliance,
            self.audit,
            self.healing,
        ]
        self._core: Any = None
        self._core_flows: list[str] = []
        self._scheduler: Any = None
        self._scheduler_flows: list[str] = []

    def _flow_specs(self) -> list[tuple[str, Any, float]]:
        """(flow_id, component, interval) — automation-flows.json 同名清單。"""
        return [
            ("permission-automation-lifecycle", self.lifecycle,
             self.lifecycle.check_interval),
            ("permission-automation-directory", self.directory_sync,
             self.directory_sync.sync_interval),
            ("permission-automation-compliance", self.compliance,
             self.compliance.check_interval),
            ("permission-automation-healing", self.healing,
             self.healing.check_interval),
            ("permission-automation-audit", self.audit,
             self.audit.interval),
        ]

    async def start(self) -> None:
        """啟動所有自動化組件。

        §1.1 自動化集中：automation core 為唯一註冊點；個別 flow 被
        kill switch 拒絕時不回落私有迴圈（否則 kill switch 可繞過）。
        """
        if self._running:
            return

        core = getattr(
            getattr(self.permission_sovereign, "app", None),
            "automation_core", None)
        if core is not None:
            self._core = core
            self._core_flows = []
            for flow_id, component, interval in self._flow_specs():
                if core.register_flow(
                    flow_id,
                    component.run_once,
                    interval_s=interval,
                    pausable=True,
                ):
                    self._core_flows.append(flow_id)
            self._running = True
            _logger.info(
                "PermissionAutomationOrchestrator started via automation core "
                "(%d/%d flows registered)",
                len(self._core_flows), len(self._flow_specs()),
            )
            return

        # 單一排程者語意：無自動化核心但共享排程器在時，run_once 掛到
        # PeriodicScheduler 而非為每個元件各開一條私有迴圈。
        app = getattr(self.permission_sovereign, "app", None)
        scheduler = getattr(app, "periodic_scheduler", None)
        if scheduler is not None:
            self._scheduler = scheduler
            self._scheduler_flows = []
            for flow_id, component, interval in self._flow_specs():
                scheduler.register(
                    flow_id, interval, component.run_once, pausable=True,
                )
                self._scheduler_flows.append(flow_id)
            self._running = True
            _logger.info(
                "PermissionAutomationOrchestrator started via periodic "
                "scheduler (%d/%d flows registered)",
                len(self._scheduler_flows), len(self._flow_specs()),
            )
            return

        for component in self._components:
            await component.start()

        self._running = True
        _logger.info("PermissionAutomationOrchestrator started")

    async def stop(self) -> None:
        """停止所有自動化組件。"""
        if self._core is not None:
            for flow_id in self._core_flows:
                self._core.unregister(flow_id)
            self._core_flows = []
            self._core = None
        if self._scheduler is not None:
            for flow_id in self._scheduler_flows:
                self._scheduler.unregister(flow_id)
            self._scheduler_flows = []
            self._scheduler = None
        for component in reversed(self._components):
            await component.stop()

        self._running = False
        _logger.info("PermissionAutomationOrchestrator stopped")

    def get_system_status(self) -> dict[str, Any]:
        """獲取整體系統狀態。"""
        return {
            "lifecycle": self.lifecycle.get_stats(),
            "directory_sync": self.directory_sync.get_sync_status(),
            "compliance": self.compliance.get_risk_report(),
            "audit_history": self.audit.get_audit_history(10),
            "healing_degraded": self.healing.is_degraded(),
            "identity_groups": self.identity.list_active_groups(),
            "identity_directory_reconciliation": self.identity.reconcile_with_directory(),
        }

    # 代理方法 - 委派給權限主宰
    def register_grant(self, *args, **kwargs) -> Any:
        return self.lifecycle.register_grant(*args, **kwargs)

    def revoke_grant(self, *args, **kwargs) -> Any:
        return self.lifecycle.revoke_grant(*args, **kwargs)

    def register_identity_group(self, *args, **kwargs) -> Any:
        return self.identity.register_group(*args, **kwargs)


__all__ = [
    "PermissionGrantState",
    "ComplianceSeverity",
    "PermissionGrant",
    "ComplianceViolation",
    "AuditSchedule",
    "PermissionLifecycleManager",
    "DirectorySyncManager",
    "ComplianceMonitor",
    "AuditScheduler",
    "SelfHealingManager",
    "IdentityGroupManager",
    "PermissionAutomationOrchestrator",
]
