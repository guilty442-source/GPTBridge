from __future__ import annotations

import asyncio
import time
from typing import Any


_STATUS_CACHE_TTL_SECONDS: float = 1.0
_status_cache: dict[int, tuple[float, dict[str, Any]]] = {}

# Global fault tracking for Xingcheng: aggregating fault evidence reads many
# small databases and journals, so the projection is cached and every push
# reuses it until the TTL expires.
_GLOBAL_FAULT_TTL_SECONDS: float = 10.0
_global_fault_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _global_fault_summary(recent_limit: int = 20) -> dict[str, Any] | None:
    """Return the cached global fault projection (never raises)."""

    now = time.monotonic()
    cached = _global_fault_cache.get("payload")
    cached_at = float(_global_fault_cache.get("at") or 0.0)
    if isinstance(cached, dict) and now - cached_at < _GLOBAL_FAULT_TTL_SECONDS:
        return cached
    try:
        from core_system.fault_analysis_service import get_fault_analysis_service

        service = get_fault_analysis_service()
        faults = service.collect_all_faults(limit=recent_limit)
        patterns = service.detect_patterns(faults)
        severity: dict[str, int] = {}
        source: dict[str, int] = {}
        unresolved = 0
        quarantined = 0
        for fault in faults:
            severity[fault.severity] = severity.get(fault.severity, 0) + 1
            source[fault.source] = source.get(fault.source, 0) + 1
            if fault.repair_outcome in ("pending", "failure"):
                unresolved += 1
            elif fault.repair_outcome == "quarantined":
                quarantined += 1
        payload = {
            "tracked": len(faults),
            "unresolved": unresolved + quarantined,
            "quarantined": quarantined,
            "severity_distribution": severity,
            "source_distribution": source,
            "top_patterns": [pattern.as_dict() for pattern in patterns[:3]],
            "recent_faults": [fault.as_dict() for fault in faults[:5]],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _global_fault_cache["at"] = now
        _global_fault_cache["payload"] = payload
        return payload
    except Exception:
        return cached if isinstance(cached, dict) else None


class RuntimeStatusService:
    """Read-only status for the main program.

    Cleanup, diagnosis, repair, quarantine, and recovery belong to the
    main-system central repair service (``tasks.central_repair``).
    """

    COMMANDS = {"app:get-runtime-status"}

    def __init__(self, app: Any) -> None:
        self.app = app

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(
        self,
        command: str,
        _payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if command == "app:get-runtime-status":
            compact = (
                isinstance(_payload, dict) and _payload.get("compact") is True
            )
            if compact:
                return "app:get-runtime-status_result", await asyncio.to_thread(
                    self.compact_status
                )
            return "app:get-runtime-status_result", await asyncio.to_thread(
                self.startup_status
            )
        raise ValueError(f"Unknown runtime status command: {command}")

    def compact_status(self, snapshot: Any | None = None) -> dict[str, Any]:
        """Small readiness/control projection for frequent consumers.

        The full ``startup_status`` snapshot aggregates every sovereign tree,
        learning store, and dependency probe and can exceed several hundred
        kilobytes; pushing that every two seconds saturates the IPC channel
        and the UI.  Frequent consumers (status pushes, socket readiness
        pings) receive this compact projection and fetch the full snapshot
        on demand.
        """
        from .readiness_gate import ReadinessGate

        if snapshot is None:
            snapshot = ReadinessGate(self.app).evaluate()
        result: dict[str, Any] = {
            "ok": snapshot.overall_ready,
            "backend": snapshot.runtime_state,
            "version": str(getattr(self.app, "version", "0.0.0")),
            "runtime_scope": getattr(
                getattr(self.app, "command_router", None), "scope", "starting"
            ),
            "message": (
                "runtime status ok" if snapshot.overall_ready else "runtime starting"
            ),
            "maintenance_ready": bool(
                getattr(self.app, "maintenance_ready", False)
            ),
            **snapshot.as_dict(),
        }
        try:
            from core_system.auto_action_policy import (
                read_actionable_pending_actions,
                read_automation_switches,
            )

            project_root = getattr(self.app, "project_root", None)
            actions = (
                read_actionable_pending_actions(project_root)
                if project_root
                else []
            )
            result["automation_switches"] = read_automation_switches(project_root)
            result["pending_action_count"] = len(actions)
            actionable = actions
            repairs = [
                action for action in actionable if action.get("kind") == "repair"
            ]
            result["pending_action_cardinality"] = {
                "mode": (
                    "MULTI_FAULT"
                    if len(repairs) >= 2
                    else "SINGLE_FAULT"
                    if len(repairs) == 1
                    else "NO_FAULT"
                ),
                "unresolved": len(actionable),
                "fault_count": len(repairs),
                "update_count": len(
                    [
                        action
                        for action in actionable
                        if action.get("kind") == "update"
                    ]
                ),
            }
        except Exception:
            pass
        reanchor = getattr(self.app, "authority_reanchor_service", None)
        if reanchor is not None and hasattr(reanchor, "get_status"):
            try:
                result["authority_reanchor"] = reanchor.get_status()
            except Exception:
                pass
        # Per-module automation isolation report (one entry per unit).
        try:
            from core_system.module_automation_registry import (
                module_automation_status,
            )

            result["automation_modules"] = module_automation_status(self.app)
        except Exception:
            pass
        # Global fault tracking (Xingcheng global review): all modules.
        global_faults = _global_fault_summary()
        if global_faults is not None:
            result["global_faults"] = global_faults
        try:
            from tasks.resource_governor_signal import governor_mode

            result["resource_mode"] = governor_mode()
        except Exception:
            result.setdefault("resource_mode", {})
        try:
            from core_system.xingcheng_native_model_runtime import native_model_status

            result["xingcheng_native_model_runtime"] = native_model_status()
        except Exception:
            result["xingcheng_native_model_runtime"] = {
                "state": "unavailable", "running": False, "available": False
            }
        return result

    def startup_status(self) -> dict[str, Any]:
        """Build the full runtime status payload (cached for one second).

        The UI requests this command continuously, and every build
        aggregates all sovereign status trees, integrity checks, and
        dependency probes on the IPC event loop.  A short TTL collapses
        the duplicated work while keeping the surface current enough for
        status display; callers receive their own top-level mapping so a
        consumer cannot pollute the cached snapshot.
        """
        now = time.monotonic()
        cache_key = id(self.app)
        cached = _status_cache.get(cache_key)
        if cached is not None and now - cached[0] < _STATUS_CACHE_TTL_SECONDS:
            return dict(cached[1])
        from .readiness_gate import ReadinessGate

        readiness = ReadinessGate(self.app).evaluate()
        result: dict[str, Any] = {
            "ok": readiness.overall_ready,
            "backend": readiness.runtime_state,
            "version": str(getattr(self.app, "version", "0.0.0")),
            "runtime_scope": getattr(
                getattr(self.app, "command_router", None), "scope", "starting"
            ),
            "message": "runtime status ok",
            **readiness.as_dict(),
        }
        get_startup_status = getattr(self.app, "get_startup_status", None)
        if callable(get_startup_status):
            result.update(get_startup_status())
        try:
            from core_system.xingcheng_native_model_runtime import native_model_status

            result["xingcheng_native_model_runtime"] = native_model_status()
        except Exception:
            result["xingcheng_native_model_runtime"] = {
                "state": "unavailable", "running": False, "available": False
            }
        # User-confirmation queue (Xingcheng assistant panel).  Per-item
        # fault/update approvals plus the persisted A366 switches.  Only
        # live actionable items are presented; terminal/reconciled records
        # remain in the durable queue as evidence.
        try:
            from core_system.auto_action_policy import (
                read_actionable_pending_actions,
                read_automation_switches,
            )

            project_root = getattr(self.app, "project_root", None)
            actions = (
                read_actionable_pending_actions(project_root)
                if project_root
                else []
            )
            result["pending_actions"] = actions
            result["automation_switches"] = read_automation_switches(project_root)
            result["pending_action_cardinality"] = {
                "mode": (
                    "MULTI_FAULT"
                    if len(
                        [
                            action
                            for action in actions
                            if action.get("kind") == "repair"
                        ]
                    )
                    >= 2
                    else "SINGLE_FAULT"
                    if any(
                        action.get("kind") == "repair" for action in actions
                    )
                    else "NO_FAULT"
                ),
                "unresolved": len(actions),
            }
        except Exception:
            result.setdefault("pending_actions", [])
            result.setdefault("automation_switches", {})
            result.setdefault(
                "pending_action_cardinality",
                {"mode": "NO_FAULT", "unresolved": 0},
            )
        # Governed authority re-anchor status (codex updates without restart).
        reanchor = getattr(self.app, "authority_reanchor_service", None)
        if reanchor is not None and hasattr(reanchor, "get_status"):
            try:
                result["authority_reanchor"] = reanchor.get_status()
            except Exception:
                pass
        # Global fault tracking (Xingcheng global review): all modules.
        global_faults = _global_fault_summary(recent_limit=50)
        if global_faults is not None:
            result["global_faults"] = global_faults
        try:
            from tasks.resource_governor_signal import governor_mode

            result["resource_mode"] = governor_mode()
        except Exception:
            result.setdefault("resource_mode", {})
        _status_cache[cache_key] = (now, result)
        return dict(result)
