"""Tool separation signal functions — A184/E159.

Information-layer signal payloads for separation violations.  These
functions produce signal dicts that must be routed through the information
layer; they never perform repair, reset, or process termination.
"""

from __future__ import annotations

from typing import Any

from core_system.tool_separation_types import SeparationReport


def separation_violation_signal(
    report: SeparationReport,
) -> dict[str, Any]:
    """Produce an information-layer signal for separation violations (A184/E159).

    Per A184: ``FAILURE:main-system-crash/restart/update/repair/close does-not-
    stop/reset/rollback/corrupt-active-tool; TOOL-crash/restart/update/repair/
    close does-not-degrade-main-system-or-other-tools``.  This function
    produces the signal payload that must be routed through the information
    layer; it never performs repair, reset, or process termination.
    """
    return {
        "signal_type": "tool-separation-violation",
        "authority": "signal-only",
        "basis": "A184/E159",
        "tool_id": report.tool_id,
        "ok": report.ok,
        "violations": [v.as_dict() for v in report.violations],
        "verified_dimensions": list(report.verified_dimensions),
        "action_required": "freeze-mutation+preserve-active-code+route-to-sovereign-decision",
        "cross_reset": False,
        "cascade_kill": False,
    }


def reciprocal_isolation_signal(
    isolation_report: dict[str, Any],
) -> dict[str, Any]:
    """Produce an information-layer signal for reciprocal isolation (A184/E159).

    Per A184: ``INFORMATION-LAYER-LOSS:only-cross-individual-communication/
    status/control becomes-unavailable+each-current-runtime-continues-under-
    own-local-safe-policy``.  This function produces the signal payload that
    must be routed through the information layer; it never performs repair,
    reset, or process termination.
    """
    return {
        "signal_type": "reciprocal-isolation",
        "authority": "signal-only",
        "basis": "A184/E159",
        "ok": isolation_report.get("ok", False),
        "individuals": isolation_report.get("individuals", {}),
        "violations": isolation_report.get("violations", []),
        "cascade": False,
        "information_layer_loss": "communication-fails-closed+runtimes-continue",
        "recovery": "owner-only+bounded+verified+stability-window",
        "global_kill": False,
        "shared_lifetime": False,
        "cross_reset": False,
    }
