"""Startup lifecycle — signals (A192-E170).

Signal functions for projection gaps and window host status.  Split from
``startup_lifecycle`` for A185/E160 source-size compliance.
"""

from __future__ import annotations

from typing import Any

from core_system.startup_lifecycle_types import WindowHostIdentity

# ---------------------------------------------------------------------------
# A195/E169 — Projection gap signal
# ---------------------------------------------------------------------------

def projection_gap_signal(
    gap_type: str,
    *,
    entity_id: str = "",
) -> dict[str, Any]:
    """Produce a signal for a projection gap (A195: GAP).

    Per A195: ``GAP:sequence-gap/revision-gap/hash-mismatch/unknown-event/
    contract-mismatch/backend-generation-change/release-change=>invalidate-
    affected-projection+block-stale-state-changing-actions+request-
    authoritative-scoped-snapshot+replay-after-snapshot-cursor``.
    """
    return {
        "signal_type": "projection-gap",
        "authority": "signal-only",
        "basis": "A195/E169",
        "gap_type": gap_type,
        "entity_id": entity_id,
        "action": "invalidate+block-stale+scoped-snapshot+replay",
        "direct_ui_mutation": False,
        "polling_primary": False,
        "manual_refresh": False,
    }


# ---------------------------------------------------------------------------
# A196/E170 — Window host status signal
# ---------------------------------------------------------------------------

def window_host_signal(
    identity: WindowHostIdentity,
    *,
    operation: str = "",
) -> dict[str, Any]:
    """Produce a signal for window host status (A196/E170).

    Per A196: ``STATUS:information-layer publishes host-id+generation+session+
    release+renderer-state without-transport-handle``.
    """
    return {
        "signal_type": "window-host-status",
        "authority": "signal-only",
        "basis": "A196/E170",
        "application_entity_id": identity.application_entity_id,
        "window_host_process_id": identity.window_host_process_id,
        "window_generation": identity.window_generation,
        "session_id": identity.session_id,
        "release_id": identity.release_id,
        "operation": operation,
        "never_two_live_hosts": True,
        "cross_application_sharing": False,
    }
