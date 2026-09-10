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

All functions are **read-only verification and signal production**.  They
never start/stop processes, mutate state, or bypass the information layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# A192/E167 — Startup sovereign capabilities (max 3)
# ---------------------------------------------------------------------------

STARTUP_SOVEREIGN_CAPABILITIES: Final[tuple[str, ...]] = (
    "bootstrap-and-authority-readiness-orchestration",
    "certified-dependency-DAG-and-sovereign-activation",
    "readiness-handoff-or-owned-failure-rollback",
)

MAX_STARTUP_CAPABILITIES: Final[int] = 3


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

OFFICIAL_PLATFORM_ENTRY: Final[str] = "gptbridge-start"

ENTRY_ROLE: Final[tuple[str, ...]] = (
    "mechanical-process-wake-only",
    "no-governance-decision",
    "no-system-decision",
    "no-business-decision",
)

LAUNCHER_DUTIES: Final[tuple[str, ...]] = (
    "show-restore-focus-reload-formal-UI",
    "display-last-confirmed-state-as-stale-until-live-snapshot",
    "subscribe-to-startup-events",
    "submit-typed-user-start-cancel-shutdown-intent",
)

LAUNCHER_FORBIDDEN: Final[tuple[str, ...]] = (
    "environment-checks",
    "governance-checks",
    "dependency-checks",
    "direct-backend-start",
    "direct-database-start",
    "direct-model-start",
    "direct-tool-start",
    "owning-startup-state",
    "blocking-UI-until-backend-ready",
)


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

HANDOFF_FLOW: Final[tuple[str, ...]] = (
    "startup-sovereign",
    "information-layer",
    "system-runtime-sovereign",
)

HANDOFF_PROOF_FIELDS: Final[tuple[str, ...]] = (
    "startup-generation",
    "release-id",
    "codex-identity",
    "permission-directory-version",
    "information-layer-generation",
    "activated-node-set",
    "dependency-evidence",
    "resource-allocation",
    "health-baseline",
    "timestamp",
    "expiry",
    "content-hash",
)


@dataclass(frozen=True)
class ReadinessProof:
    """Startup readiness handoff proof (A194: PROOF).

    Per A194: ``PROOF:startup-generation+release-id+codex-identity+permission-
    directory-version+information-layer-generation+activated-node-set+
    dependency-evidence+resource-allocation+health-baseline+timestamp+expiry+
    content-hash``.
    """

    startup_generation: str
    release_id: str
    codex_identity: str
    permission_directory_version: str
    information_layer_generation: str
    activated_node_set: tuple[str, ...]
    dependency_evidence: dict[str, Any] = field(default_factory=dict)
    resource_allocation: dict[str, Any] = field(default_factory=dict)
    health_baseline: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    expiry: str = ""
    content_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_all_fields(self) -> bool:
        """Check all required proof fields are present (A194: PROOF)."""
        return all(
            getattr(self, field_name.replace("-", "_"))
            for field_name in HANDOFF_PROOF_FIELDS
        )


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


# ---------------------------------------------------------------------------
# A195/E169 — Backend state to frontend projection synchronization
# ---------------------------------------------------------------------------

CHANGE_CLASSES: Final[tuple[str, ...]] = (
    "STATE-CHANGE",
    "UI-ARTIFACT-CHANGE",
    "CONTRACT-CHANGE",
)

EVENT_FIELDS: Final[tuple[str, ...]] = (
    "entity-id",
    "entity-type",
    "operation",
    "authoritative-revision",
    "previous-revision",
    "changed-field-allowlist",
    "invalidation-keys",
    "state-hash",
    "backend-generation",
    "release-id",
    "contract-version",
    "sequence",
    "correlation-id",
    "committed-at",
)


@dataclass(frozen=True)
class StateEvent:
    """A transactional outbox state event (A195: EVENT).

    Per A195: ``STATE-CHANGE:commit-authoritative-state and append-
    transactional-outbox-event in-one-atomic-boundary``.
    """

    entity_id: str
    entity_type: str
    operation: str
    authoritative_revision: int
    previous_revision: int
    changed_field_allowlist: tuple[str, ...]
    invalidation_keys: tuple[str, ...]
    state_hash: str
    backend_generation: str
    release_id: str
    contract_version: str
    sequence: int
    correlation_id: str
    committed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectionSyncCheck:
    """Result of verifying backend-frontend projection sync (A195)."""

    ok: bool
    snapshot_revision: int
    cursor_revision: int
    state_hash_match: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_projection_sync(
    *,
    authoritative_revision: int,
    projection_revision: int,
    cursor_revision: int,
    authoritative_hash: str,
    projection_hash: str,
) -> ProjectionSyncCheck:
    """Verify backend-frontend projection synchronization (A195/E169).

    Per A195: ``READY:projection-revision/state-hash/cursor match-
    authoritative-snapshot and subscription-current``.
    """
    violations: list[str] = []
    if projection_revision != authoritative_revision:
        violations.append(
            f"revision-mismatch:projection={projection_revision}!=auth={authoritative_revision}"
        )
    if cursor_revision != authoritative_revision:
        violations.append(
            f"cursor-mismatch:cursor={cursor_revision}!=auth={authoritative_revision}"
        )
    hash_match = projection_hash == authoritative_hash
    if not hash_match:
        violations.append("state-hash-mismatch")
    return ProjectionSyncCheck(
        ok=len(violations) == 0,
        snapshot_revision=authoritative_revision,
        cursor_revision=cursor_revision,
        state_hash_match=hash_match,
        violations=tuple(violations),
    )


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
# A196/E170 — Stable single window process per application session
# ---------------------------------------------------------------------------

WINDOW_HOST_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "application-entity-id",
    "window-host-process-id",
    "window-generation",
    "session-id",
    "release-id",
)

# A196: operations that must reuse the same window host (not spawn new)
SAME_HOST_OPERATIONS: Final[tuple[str, ...]] = (
    "F5-reload",
    "renderer-reload",
    "backend-state-change",
    "reconnect",
    "backend-restart",
    "repair",
    "runtime-compatible-hot-update",
)

# A196: valid reasons for host replacement
HOST_REPLACEMENT_REASONS: Final[tuple[str, ...]] = (
    "verified-host-crash",
    "unrecoverable-host-corruption",
    "runtime-incompatible-certified-release",
    "explicit-user-full-application-restart",
    "os-session-termination",
)


@dataclass(frozen=True)
class WindowHostIdentity:
    """Window host process identity (A196: IDENTITY).

    Per A196: ``IDENTITY:{application-entity-id,window-host-process-id,
    window-generation,session-id,release-id}``.
    """

    application_entity_id: str
    window_host_process_id: int
    window_generation: int
    session_id: str
    release_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WindowHostCheck:
    """Result of verifying window host continuity (A196)."""

    ok: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_window_host_continuity(
    *,
    operation: str,
    host_changed: bool,
    replacement_reason: str = "",
) -> WindowHostCheck:
    """Verify window host process continuity (A196/E170).

    Per A196: ``INVARIANT:during-one-live-application-session exactly-one-
    window-host-process owns-all-that-application-windows``.
    """
    violations: list[str] = []

    if operation in SAME_HOST_OPERATIONS and host_changed:
        violations.append(
            f"host-changed-on-{operation}:must-reuse-same-host"
        )

    if host_changed and replacement_reason:
        if replacement_reason not in HOST_REPLACEMENT_REASONS:
            violations.append(
                f"invalid-replacement-reason:{replacement_reason}"
            )

    return WindowHostCheck(
        ok=len(violations) == 0,
        violations=tuple(violations),
    )


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
    "verify_startup_sovereign_capabilities",
    "verify_window_host_continuity",
    "window_host_signal",
]
