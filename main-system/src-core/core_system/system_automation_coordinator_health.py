"""System automation coordinator — health aggregation mixin.

Extracted from SystemAutomationCoordinator: system health aggregation,
degradation detection, and xingcheng evidence projection.
"""

from __future__ import annotations

import logging
from typing import Any

from .system_automation_coordinator_constants import _SOVEREIGN_ATTRS

_logger = logging.getLogger("gptbridge.system_automation")


class SystemAutomationHealthMixin:
    """Health aggregation and degradation detection for SystemAutomationCoordinator."""

    def _is_sovereign_automated(self, sovereign: Any) -> bool:
        """Check if a sovereign has its automation loop running."""
        # Check for auto loop task (sync, runtime, xingcheng).
        auto_task = getattr(sovereign, "_auto_loop_task", None)
        if auto_task is not None and not auto_task.done():
            return True
        # Check for autonomy task (decision sovereign).
        autonomy_task = getattr(sovereign, "_autonomy_task", None)
        if autonomy_task is not None and not autonomy_task.done():
            return True
        # Check for permission automation orchestrator.
        automation = getattr(sovereign, "_automation", None)
        if automation is not None and getattr(automation, "_running", False):
            return True
        return False

    def _aggregate_system_health(self) -> dict[str, Any]:
        """Aggregate health status from all managed sovereigns."""
        sovereigns_health: dict[str, Any] = {}
        all_started = True
        all_automated = True
        any_degraded = False
        any_critical = False

        for attr in _SOVEREIGN_ATTRS:
            sov = getattr(self.app, attr, None)  # type: ignore[attr-defined]
            if sov is None:
                sovereigns_health[attr] = {
                    "state": "missing",
                    "started": False,
                    "automated": False,
                }
                all_started = False
                all_automated = False
                any_critical = True
                continue

            started = bool(getattr(sov, "_started", False))
            automated = self._is_sovereign_automated(sov)
            sov_id = getattr(sov, "sovereign_id", attr)

            live_status: dict[str, Any] = {}
            if hasattr(sov, "live_status"):
                try:
                    live_status = sov.live_status() or {}
                except Exception:
                    live_status = {}

            if not started:
                state = "stopped"
                any_critical = True
            else:
                runtime_state = live_status.get("runtime_state", "")
                if runtime_state in ("degraded", "failed", "dead"):
                    state = "degraded"
                    any_degraded = True
                elif runtime_state in ("serving", "ready", "active"):
                    state = "healthy"
                else:
                    state = "started"

            sovereigns_health[attr] = {
                "sovereign_id": sov_id,
                "state": state,
                "started": started,
                "automated": automated,
                "runtime_state": runtime_state,
            }

            if not started:
                all_started = False
            if not automated:
                all_automated = False

        if any_critical:
            overall = "critical"
        elif any_degraded:
            overall = "degraded"
        elif all_started and all_automated:
            overall = "fully-automated"
        elif all_started:
            overall = "running"
        else:
            overall = "partial"

        return {
            "aggregated_at": self._iso_now(),  # type: ignore[attr-defined]
            "overall_state": overall,
            "all_started": all_started,
            "all_automated": all_automated,
            "sovereigns": sovereigns_health,
            "coordinator_version": self.VERSION,  # type: ignore[attr-defined]
        }

    def _detect_degradation(
        self, health: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Detect cross-sovereign degradation that needs routing."""
        overall = health.get("overall_state", "healthy")
        if overall in ("healthy", "fully-automated", "running"):
            return None

        degraded_sovereigns: list[dict[str, Any]] = []
        for attr, sov_health in health.get("sovereigns", {}).items():
            state = sov_health.get("state", "unknown")
            if state in ("degraded", "stopped", "critical"):
                degraded_sovereigns.append({
                    "sovereign": attr,
                    "sovereign_id": sov_health.get("sovereign_id", attr),
                    "state": state,
                    "runtime_state": sov_health.get("runtime_state", ""),
                })

        if not degraded_sovereigns:
            return None

        return {
            "detected_at": self._iso_now(),  # type: ignore[attr-defined]
            "type": "cross-sovereign-degradation",
            "overall_state": overall,
            "degraded_sovereigns": degraded_sovereigns,
            "route_to": "decision-sovereign.repair-decision",
            "authority": "system-automation-coordinator",
        }

    def _emit_xingcheng_evidence(self, health: dict[str, Any]) -> None:
        """A140/A146: write the filtered system-evidence projection."""
        import json
        from pathlib import Path

        xingcheng = getattr(self.app, "xingcheng_sovereign", None)  # type: ignore[attr-defined]
        domain_root = getattr(xingcheng, "_owned_domain_root", None)
        if not domain_root:
            return

        items: list[dict[str, Any]] = [
            {
                "component": "system-overall",
                "state": health.get("overall_state"),
                "detail": None,
            }
        ]
        for attr, sov_health in health.get("sovereigns", {}).items():
            items.append({
                "component": sov_health.get("sovereign_id") or attr,
                "state": sov_health.get("state") or "unknown",
                "detail": None,
            })
        projection = {
            "projected_at": self._iso_now(),  # type: ignore[attr-defined]
            "channel": "information-layer",
            "redacted": True,
            "items": items,
        }
        try:
            target = (
                Path(domain_root) / "governance" / "system-evidence.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(projection, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except (OSError, UnicodeError) as error:
            _logger.warning("xingcheng evidence projection failed: %s", error)
