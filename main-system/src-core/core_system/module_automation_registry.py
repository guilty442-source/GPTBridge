"""Module automation registry — per-module automation isolation.

Every automation unit (hot-reload watcher, update manager, authority
re-anchor, daily cleaner, repair chain) runs independently.  This registry
reports each module's automation state separately so one failing unit is
visible and contained; a unit that raises is reported as failed and never
breaks the status of another unit or the main system.
"""

from __future__ import annotations

from typing import Any, Final

# (unit id = module code, app attribute, display label)
AUTOMATION_UNITS: Final[tuple[tuple[str, str, str], ...]] = (
    ("hot-reload", "hot_reload_watcher", "hot-reload"),
    ("update-manager", "update_manager", "update-manager"),
    ("authority-reanchor", "authority_reanchor_service", "authority-reanchor"),
    ("daily-cleaner", "daily_global_cleaner_service", "daily-cleaner"),
    ("repair-chain", "maintenance_sovereign", "repair-chain"),
)


def _unit_state(service: Any) -> tuple[str, str]:
    """Return (state, last_error) for one unit without ever raising."""
    if service is None:
        return "unavailable", ""
    last_error = ""
    for method_name in ("get_status", "status"):
        method = getattr(service, method_name, None)
        if not callable(method):
            continue
        try:
            status = method()
        except Exception as error:  # isolated: other units stay unaffected
            return "error", f"{type(error).__name__}: {error}"
        if isinstance(status, dict):
            state = status.get("state") or status.get("status") or "unknown"
            return str(state), str(status.get("last_error") or "")
        return "unknown", last_error
    return "running", last_error


def module_automation_status(app: Any) -> list[dict[str, Any]]:
    """Per-module automation isolation report (one entry per unit)."""
    report: list[dict[str, Any]] = []
    for unit_id, attribute, label in AUTOMATION_UNITS:
        service = getattr(app, attribute, None)
        state, last_error = _unit_state(service)
        report.append(
            {
                "unit": unit_id,
                "label": label,
                "present": service is not None,
                "state": state,
                "last_error": last_error,
                "isolated": True,
            }
        )
    return report


__all__ = ["AUTOMATION_UNITS", "module_automation_status"]
