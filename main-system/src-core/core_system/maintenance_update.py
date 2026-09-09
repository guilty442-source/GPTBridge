"""Maintenance Sovereign — update / hot-reload / repair / fault / backup mixin.

Extracted from ``maintenance_sovereign`` to keep each module focused and
under 500 lines.  Preserves ``execute_hot_reload`` and third-party update
coordination.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .codex_decision import decision_basis


class MaintenanceUpdateMixin:
    """Update-management, automatic-repair, fault-determination and backup
    duty surfaces for the Maintenance Sovereign.

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._hot_update`` / ``self._repair_service``
      * ``self.ROLE``
    """

    # ------------------------------------------------------------------
    # Update / automatic-repair / fault-determination / backup surfaces
    # ------------------------------------------------------------------

    def _update_status(self) -> dict[str, Any]:
        """Update duty — supervises the version-gated hot-update boundary and
        the system-wide hot-reload capability."""

        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {"duty": "update-management", "owner": self.ROLE, "enabled": False}
        get_status = getattr(hot_update, "status", None)
        if callable(get_status):
            try:
                return {"duty": "update-management", "owner": self.ROLE, **get_status()}
            except Exception:
                return {"duty": "update-management", "owner": self.ROLE, "available": True}
        return {"duty": "update-management", "owner": self.ROLE, "available": True}

    async def _notify_ui(self, event: str, payload: dict[str, Any]) -> int:
        shells = getattr(self.app, "_active_ui_shells", None) or set()
        if not shells:
            return 0
        count = 0
        for shell in list(shells):
            send = getattr(shell, "send_event", None)
            if not callable(send):
                continue
            try:
                await send(event, payload)
                count += 1
            except Exception:
                pass
        return count

    async def execute_hot_reload(
        self,
        *,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> dict[str, Any]:
        """Coordinate a system-wide hot-reload of governed backend modules.

        Hot-reload is a maintenance operation under the update-management
        duty (A24/E8).  It reloads already-loaded Python modules in-place so
        source edits to governed backend code take effect without a full
        process restart.  Governance authorization is required; the scope is
        system-wide (all backend src roots, not just main-system/src-core).

        After a successful reload the self-maintenance stability check is
        re-run and the frontend is notified so it can refresh in sync.
        """
        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {
                "ok": False,
                "duty": "update-management",
                "error": "hot-update-service-unavailable",
            }
        reload_modules = getattr(hot_update, "reload_modules", None)
        if not callable(reload_modules):
            return {
                "ok": False,
                "duty": "update-management",
                "error": "hot-reload-not-supported",
            }
        governance = getattr(self.app, "governance", None)
        report = await asyncio.to_thread(
            reload_modules,
            governance=governance,
            approval_token=approval_token,
            modules=modules,
        )

        # Sync the frontend so it can refresh against the newly loaded backend.
        notified = await self._notify_ui(
            "maintenance:hot-reload-completed",
            {
                "ok": report.ok,
                "reloaded_count": len(report.reloaded),
                "skipped_count": len(report.skipped),
                "errors": list(report.errors)[:8],
            },
        )

        # A successful reload is an accepted runtime revision.  Automatic
        # repair must not run from this path or replace that accepted source.
        auto_repair: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "reason": "hot-reload-revision-protected",
        }

        return {
            "ok": report.ok,
            "duty": "update-management",
            "operation": "hot-reload",
            "authority": self.ROLE,
            "scope": "system-wide",
            "reloaded": list(report.reloaded),
            "skipped": list(report.skipped),
            "errors": list(report.errors),
            "ui_notified": notified,
            "auto_repair": auto_repair,
        }

    def _third_party_update_executor(self) -> Any:
        system_sovereign = getattr(self.app, "system_sovereign_service", None)
        if system_sovereign is None:
            return None
        return getattr(system_sovereign, "third_party_sovereign", None)

    async def execute_third_party_update(
        self, tool_id: str, *, approval_token: str | None = None
    ) -> Any:
        """Manage one update and delegate only its execution."""
        executor = self._third_party_update_executor()
        if executor is None:
            raise RuntimeError("third-party update executor unavailable")
        return await executor.apply_approved_update(
            tool_id, approval_token=approval_token
        )

    async def execute_auto_third_party_updates(
        self, *, approval_token: str, only_available: bool = True
    ) -> dict[str, Any]:
        """Manage approved automatic updates and delegate their execution."""
        executor = self._third_party_update_executor()
        if executor is None:
            raise RuntimeError("third-party update executor unavailable")
        return await executor.apply_approved_auto_updates(
            approval_token=approval_token, only_available=only_available
        )

    def _automatic_repair_status(self) -> dict[str, Any]:
        """Automatic-repair duty — coordinates the governed repair service."""

        repair = self._repair_service
        if repair is None:
            return {
                "duty": "automatic-repair",
                "enabled": False,
                "delegation": "governed-executor-only",
            }
        get_status = getattr(repair, "status", None)
        if callable(get_status):
            try:
                return {"duty": "automatic-repair", **get_status()}
            except Exception:
                return {"duty": "automatic-repair", "enabled": True}
        return {"duty": "automatic-repair", "enabled": True}

    def _fault_determination_status(self) -> dict[str, Any]:
        """Fault-determination duty — surfaces the repair planner readiness."""

        repair = self._repair_service
        return {
            "duty": "fault-determination",
            "enabled": repair is not None,
            "decision": decision_basis(self._maintenance_area())["edicts"],
        }

    def _backup_status(self) -> dict[str, Any]:
        """Backup duty — coordinates governed backup/backup-extraction executor."""

        repair = self._repair_service
        backup_enabled = repair is not None and hasattr(repair, "plan_repair")
        return {
            "duty": "backup",
            "enabled": bool(backup_enabled),
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Helper for accessing module-level constant without circular import
    # ------------------------------------------------------------------

    def _maintenance_area(self):
        from .maintenance_sovereign import _MAINTENANCE_SOVEREIGN
        return _MAINTENANCE_SOVEREIGN.area
