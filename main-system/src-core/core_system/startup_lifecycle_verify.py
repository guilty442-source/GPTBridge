"""Startup lifecycle — verification (A192-E170).

Verification functions for startup sovereign capabilities, entry launcher,
and readiness handoff.  Split from ``startup_lifecycle`` for A185/E160
source-size compliance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core_system.startup_lifecycle_types import (
    HANDOFF_FLOW,
    MAX_STARTUP_CAPABILITIES,
    OFFICIAL_PLATFORM_ENTRY,
    ReadinessProof,
)

# ---------------------------------------------------------------------------
# A192/E167 — Startup sovereign capabilities (max 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartupSovereignCapabilityCheck:
    """Result of verifying startup sovereign capability count (A192)."""

    ok: bool
    declared_capabilities: tuple[str, ...]
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_startup_sovereign_capabilities(
    declared: tuple[str, ...],
) -> StartupSovereignCapabilityCheck:
    """Verify startup sovereign has at most 3 capabilities (A192/E167).

    Per A192: ``MAX-CAPABILITIES:3`` and ``FORBID:more-than-three-startup-
    capabilities+business-decision+permission-decision+health-classification+
    runtime-control-after-handoff``.
    """
    violations: list[str] = []
    if len(declared) > MAX_STARTUP_CAPABILITIES:
        violations.append(f"exceeds-max-capabilities:{len(declared)}>{MAX_STARTUP_CAPABILITIES}")
    forbidden = {
        "business-decision",
        "permission-decision",
        "health-classification",
        "runtime-control-after-handoff",
        "maintenance",
        "repair",
        "code-change",
        "tool-auto-start",
    }
    for cap in declared:
        if cap in forbidden:
            violations.append(f"forbidden-capability:{cap}")
    return StartupSovereignCapabilityCheck(
        ok=len(violations) == 0,
        declared_capabilities=declared,
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# A193/E168 — Official startup entry and UI launcher
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryLauncherCheck:
    """Result of verifying official entry and UI launcher behavior (A193)."""

    ok: bool
    entry: str
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_entry_launcher(
    *,
    entry_id: str,
    launcher_performs_checks: bool = False,
    launcher_direct_backend: bool = False,
    launcher_blocks_ui: bool = False,
    visible_console: bool = False,
    duplicate_runtime: bool = False,
) -> EntryLauncherCheck:
    """Verify official entry and UI launcher behavior (A193/E168).

    Per A193: ``ENTRY-ROLE:mechanical-process-wake-only+no-governance/system/
    business-decision`` and ``LAUNCHER-SYSTEM-POWER:none``.
    """
    violations: list[str] = []
    if entry_id != OFFICIAL_PLATFORM_ENTRY:
        violations.append(f"unofficial-entry:{entry_id}")
    if launcher_performs_checks:
        violations.append("launcher-performs-environment/governance/dependency-checks")
    if launcher_direct_backend:
        violations.append("launcher-direct-backend-start")
    if launcher_blocks_ui:
        violations.append("launcher-blocks-UI-until-backend-ready")
    if visible_console:
        violations.append("visible-console")
    if duplicate_runtime:
        violations.append("duplicate-runtime")
    return EntryLauncherCheck(
        ok=len(violations) == 0,
        entry=entry_id,
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# A194/E169 — Startup readiness handoff and lifecycle finality
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HandoffCheck:
    """Result of verifying startup readiness handoff (A194)."""

    ok: bool
    acknowledged: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_readiness_handoff(
    proof: ReadinessProof | None,
    *,
    acknowledged: bool = False,
) -> HandoffCheck:
    """Verify startup readiness handoff (A194/E169).

    Per A194: ``BEFORE-ACK:startup-sovereign owns-startup-generation only;
    AFTER-ACK:system-runtime-sovereign owns-running-lifecycle and startup-
    sovereign cannot issue runtime commands``.
    """
    violations: list[str] = []
    if proof is None:
        violations.append("missing-readiness-proof")
    elif not proof.has_all_fields:
        violations.append("incomplete-readiness-proof")
    if acknowledged and proof is None:
        violations.append("ack-without-proof")
    return HandoffCheck(
        ok=len(violations) == 0,
        acknowledged=acknowledged,
        violations=tuple(violations),
    )
