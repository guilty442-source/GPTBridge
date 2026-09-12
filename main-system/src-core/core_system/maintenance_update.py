"""Maintenance Sovereign — update / repair / fault / backup health-monitoring
mixin.

Per the amended Governance Codex (A125/E102), the maintenance sovereign's
duties are ``monitor-system-health``, ``preserve-system-health`` and
``maintain-system`` (health-only scope, A154).  The specific maintenance
actions (update, repair, backup) are **subordinate to these three duties**
and **delegated to governed executors** (E102).  This mixin surfaces the
read-only health status of those delegated maintenance actions so the
maintenance sovereign can monitor system health.

Authority boundaries (A152/A154/E127/E128):
  * update / hot-reload  → runtime action, owned by runtime-sovereign (E127)
  * repair decision       → decision-sovereign (A152)
  * code change           → release-update-sync-sub-sovereign (E127/A309/A322)
  * backup coordination   → delegated governed executor (E102)

The health-maintenance-test sub-sovereign supervises the **health** of these
boundaries (read-only); it never owns the decision or execution.

Extracted from ``maintenance_sovereign`` (retired, A302/A323) to keep each
module focused and under 500 lines.
"""

from __future__ import annotations

from typing import Any

from .codex_decision import decision_basis


class MaintenanceUpdateMixin:
    """Health-monitoring surfaces for delegated maintenance actions.

    These methods report the read-only health status of update, repair,
    fault and backup boundaries.  The maintenance sovereign monitors
    system health (A125/E102); the decisions and execution are owned by
    other sovereigns (A152/A154/E127/E128).

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._hot_update`` / ``self._repair_service``
      * ``self.ROLE``
    """

    # ------------------------------------------------------------------
    # UI notification helper (shared with MaintenanceRepairChainMixin)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Health-monitoring surfaces (read-only, delegated actions)
    # ------------------------------------------------------------------

    def _update_status(self) -> dict[str, Any]:
        """Update-boundary health — read-only supervision of the version-gated
        hot-update boundary and the system-wide hot-reload capability.

        Per E127 (``RUNTIME-ACTION:system-runtime``), hot-reload execution is
        owned by the runtime sub-sovereign; the maintenance sovereign
        monitors the health/readiness of the update boundary only.
        """

        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {"duty": "update-health", "owner": self.ROLE, "enabled": False}
        get_status = getattr(hot_update, "status", None)
        if callable(get_status):
            try:
                return {"duty": "update-health", "owner": self.ROLE, **get_status()}
            except Exception:
                return {"duty": "update-health", "owner": self.ROLE, "available": True}
        return {"duty": "update-health", "owner": self.ROLE, "available": True}

    def _automatic_repair_status(self) -> dict[str, Any]:
        """Repair-service health — read-only monitoring of the governed
        repair service readiness.

        Per A152 (``REPAIR-DECISION:decision-sovereign``), the repair
        decision is owned by the decision-sovereign; the maintenance
        sovereign monitors the health/readiness of the repair service only.
        """

        repair = self._repair_service
        if repair is None:
            return {
                "duty": "repair-health",
                "enabled": False,
                "decision_authority": "decision-sovereign",
                "delegation": "governed-executor-only",
            }
        get_status = getattr(repair, "status", None)
        if callable(get_status):
            try:
                return {
                    "duty": "repair-health",
                    "decision_authority": "decision-sovereign",
                    **get_status(),
                }
            except Exception:
                return {
                    "duty": "repair-health",
                    "enabled": True,
                    "decision_authority": "decision-sovereign",
                }
        return {
            "duty": "repair-health",
            "enabled": True,
            "decision_authority": "decision-sovereign",
        }

    def _fault_determination_status(self) -> dict[str, Any]:
        """Health-classification readiness — surfaces the repair decision
        chain readiness.

        Per A152/A154, fault determination for repair is owned by the
        decision-sovereign; the maintenance sovereign performs health
        classification only.  This surface reports the readiness of that
        chain.
        """

        repair = self._repair_service
        return {
            "duty": "health-classification",
            "enabled": repair is not None,
            "decision_authority": "decision-sovereign",
            "decision": decision_basis(self._maintenance_area())["edicts"],
        }

    def _backup_status(self) -> dict[str, Any]:
        """Backup health — read-only monitoring of governed backup readiness.

        Per E102, backup as a specific maintenance action is subordinate to
        the three health duties and delegated to governed executors.  The
        maintenance sovereign monitors backup readiness as part of
        ``preserve-system-health``.
        """

        repair = self._repair_service
        backup_enabled = repair is not None and hasattr(repair, "plan_repair")
        return {
            "duty": "backup-health",
            "enabled": bool(backup_enabled),
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Delegated third-party update execution (A128: decide-third-party-updates)
    # ------------------------------------------------------------------

    async def execute_third_party_update(
        self,
        tool_id: str,
        *,
        approval_token: str | None = None,
    ) -> Any:
        """Delegate an approved third-party update to the third-party sovereign."""
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        third_party = getattr(decision_sovereign, "third_party_sovereign", None)
        if third_party is None:
            raise RuntimeError("third-party sovereign is not available")
        apply = getattr(third_party, "apply_approved_update")
        if not callable(apply):
            raise RuntimeError("third-party sovereign does not expose apply_approved_update")
        return await apply(tool_id, approval_token=approval_token)

    async def execute_auto_third_party_updates(
        self,
        *,
        approval_token: str = "maintenance-auto",
        only_available: bool = True,
    ) -> Any:
        """Delegate approved automatic third-party updates to the third-party sovereign."""
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        third_party = getattr(decision_sovereign, "third_party_sovereign", None)
        if third_party is None:
            raise RuntimeError("third-party sovereign is not available")
        apply = getattr(third_party, "apply_approved_auto_updates")
        if not callable(apply):
            raise RuntimeError("third-party sovereign does not expose apply_approved_auto_updates")
        return await apply(approval_token=approval_token, only_available=only_available)

    # ------------------------------------------------------------------
    # Helper for accessing module-level constant without circular import
    # ------------------------------------------------------------------

    def _maintenance_area(self):
        from governance.sub_sovereigns.health_maintenance_test_sub_sovereign import _MAINTENANCE_SOVEREIGN
        return _MAINTENANCE_SOVEREIGN.area
