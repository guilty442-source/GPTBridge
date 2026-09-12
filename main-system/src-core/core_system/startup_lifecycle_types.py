"""Startup lifecycle — types and constants (A192-E170).

Constants and data structures for the startup lifecycle.  Split from
``startup_lifecycle`` for A185/E160 source-size compliance.
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


# ---------------------------------------------------------------------------
# A193/E168 — Official startup entry and UI launcher
# ---------------------------------------------------------------------------

OFFICIAL_PLATFORM_ENTRY: Final[str] = "gptbridge-start"

ENTRY_ROLE: Final[tuple[str, ...]] = (
    "mechanical-process-wake-only",
    "no-governance-decision",
    "no-decision",
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
