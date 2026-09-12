"""Startup lifecycle, entry, handoff, and state projection — A192-E170.

This module implements the startup lifecycle provisions from codex v2.71230:

  * **A192/E167** — optimized startup sovereign responsibility (max 3
    capabilities: bootstrap-readiness, certified-DAG-activation, proof-
    handoff-or-owned-rollback).
  * **A193/E168** — official startup entry and UI launcher (gptbridge-start
    mechanically wakes or attaches; UI launcher is display-only).
  * **A194/E169** — startup readiness handoff and lifecycle finality
    (startup sovereign hands off to system-runtime sovereign via proof-
    bound atomic ack; no dual owner).
  * **A195/E169** — backend state to frontend projection synchronization
    (transactional outbox + information-layer event + revision-checked
    projection + render + ack).
  * **A196/E170** — stable single window process per application session
    (one window-host-process per application session; F5/reload/reconnect
    reuse same host; replacement only on verified death).

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.startup_lifecycle_types` — constants and dataclasses.
  * :mod:`core_system.startup_lifecycle_verify` — verification functions.
  * :mod:`core_system.startup_lifecycle_signal` — signal functions.
  * :mod:`core_system.startup_lifecycle_sync` — projection and window checks.
"""

from __future__ import annotations

from core_system.startup_lifecycle_signal import (
    projection_gap_signal,
    window_host_signal,
)
from core_system.startup_lifecycle_sync import (
    ProjectionSyncCheck,
    WindowHostCheck,
    verify_projection_sync,
    verify_window_host_continuity,
)
from core_system.startup_lifecycle_types import (
    CHANGE_CLASSES,
    ENTRY_ROLE,
    EVENT_FIELDS,
    HANDOFF_FLOW,
    HANDOFF_PROOF_FIELDS,
    HOST_REPLACEMENT_REASONS,
    LAUNCHER_DUTIES,
    LAUNCHER_FORBIDDEN,
    MAX_STARTUP_CAPABILITIES,
    OFFICIAL_PLATFORM_ENTRY,
    SAME_HOST_OPERATIONS,
    STARTUP_SOVEREIGN_CAPABILITIES,
    StateEvent,
    WINDOW_HOST_IDENTITY_FIELDS,
    WindowHostIdentity,
    ReadinessProof,
)
from core_system.startup_lifecycle_verify import (
    EntryLauncherCheck,
    HandoffCheck,
    StartupSovereignCapabilityCheck,
    verify_entry_launcher,
    verify_readiness_handoff,
    verify_startup_sub_sovereign_capabilities,
)

__all__ = [
    "CHANGE_CLASSES",
    "EntryLauncherCheck",
    "EVENT_FIELDS",
    "HANDOFF_FLOW",
    "HANDOFF_PROOF_FIELDS",
    "HOST_REPLACEMENT_REASONS",
    "HandoffCheck",
    "LAUNCHER_DUTIES",
    "LAUNCHER_FORBIDDEN",
    "MAX_STARTUP_CAPABILITIES",
    "OFFICIAL_PLATFORM_ENTRY",
    "ProjectionSyncCheck",
    "ReadinessProof",
    "SAME_HOST_OPERATIONS",
    "STARTUP_SOVEREIGN_CAPABILITIES",
    "StateEvent",
    "StartupSovereignCapabilityCheck",
    "WINDOW_HOST_IDENTITY_FIELDS",
    "WindowHostCheck",
    "WindowHostIdentity",
    "ENTRY_ROLE",
    "projection_gap_signal",
    "verify_entry_launcher",
    "verify_projection_sync",
    "verify_readiness_handoff",
    "verify_startup_sub_sovereign_capabilities",
    "verify_window_host_continuity",
    "window_host_signal",
]
