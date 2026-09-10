"""Third-party governance constants and dataclasses — A197/E171, A198/E172, A199/E173.

Types and constants extracted from third_party_governance for source-size
compliance (A185/E160).  This module holds no verification or signal logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final

# ===========================================================================
# A197/E171 — Third-party sovereign optimized responsibility
# ===========================================================================

THIRD_PARTY_SOVEREIGN_CAPABILITIES: Final[tuple[str, ...]] = (
    "dependency-identity-inventory-and-provenance",
    "license-security-compatibility-and-supply-chain-risk",
    "admission-version-and-lifecycle-decision",
)

MAX_THIRD_PARTY_CAPABILITIES: Final[int] = 3

# A197: DECISION-BOUNDARY — what the third-party sovereign decides vs not
THIRD_PARTY_DECIDES: Final[tuple[str, ...]] = (
    "artifact-admissibility",
    "version-constraint-acceptability",
)

THIRD_PARTY_DOES_NOT_DECIDE: Final[tuple[str, ...]] = (
    "system-priority",
    "release-acceptance",
    "permission",
    "runtime-activation",
    "maintenance-health",
    "code-change",
)

# A197: CAPABILITY-3 decision types
THIRD_PARTY_DECISION_TYPES: Final[tuple[str, ...]] = (
    "admit",
    "deny",
    "quarantine",
    "retain",
    "upgrade-candidate",
    "downgrade-candidate",
    "replace-candidate",
    "retire",
)

# A197: FORBIDDEN actions for the third-party sovereign
THIRD_PARTY_FORBIDDEN_ACTIONS: Final[tuple[str, ...]] = (
    "direct-download",
    "install",
    "uninstall",
    "update",
    "downgrade",
    "replace",
    "delete",
    "execute",
    "activate",
    "rollback",
    "manifest-mutation",
    "lockfile-mutation",
    "source-mutation",
    "runtime-mutation",
    "data-mutation",
    "permission-self-grant",
    "release-signing",
    "health-self-certification",
    "system-priority-decision",
    "direct-network",
    "direct-subprocess",
    "direct-socket",
    "direct-database",
)

# A197: SCOPE — artifact types covered
THIRD_PARTY_SCOPE: Final[tuple[str, ...]] = (
    "packages",
    "libraries",
    "frameworks",
    "runtimes",
    "cli-tools",
    "sdks",
    "drivers",
    "build-tools",
    "binaries",
    "models-as-third-party-artifacts",
    "container-images",
    "external-service-client-dependencies",
)


@dataclass(frozen=True)
class DependencyIdentity:
    """Stable dependency identity record (A197: CAPABILITY-1).

    Per A197: ``CAPABILITY-1:assign-stable-dependency-id+classify-development/
    build/runtime/optional+record-name/vendor/source/repository/artifact-kind/
    platform/architecture/version/version-range/content-hash/signature/provenance/
    SBOM/transitive-graph/consumer-owner/declared-purpose``.
    """

    dependency_id: str
    name: str
    vendor: str
    source: str
    repository: str
    artifact_kind: str
    platform: str
    architecture: str
    version: str
    version_range: str
    content_hash: str
    signature: str
    provenance: str
    sbom: str
    transitive_graph: dict[str, Any] = field(default_factory=dict)
    consumer_owner: str = ""
    declared_purpose: str = ""
    classification: str = ""  # development | build | runtime | optional

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThirdPartyDecision:
    """Typed third-party admission decision (A197: CAPABILITY-3).

    Per A197: ``CAPABILITY-3:issue typed admit|deny|quarantine|retain|upgrade-
    candidate|downgrade-candidate|replace-candidate|retire decision with exact-
    identity/version/constraints/evidence/expiry/review-trigger``.
    """

    dependency_id: str
    decision: str  # from THIRD_PARTY_DECISION_TYPES
    version: str
    constraints: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    expiry: str = ""
    review_trigger: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_admit(self) -> bool:
        return self.decision == "admit"

    @property
    def is_deny(self) -> bool:
        return self.decision == "deny"

    @property
    def valid_decision_type(self) -> bool:
        return self.decision in THIRD_PARTY_DECISION_TYPES


@dataclass(frozen=True)
class ThirdPartyCapabilityCheck:
    """Result of verifying third-party sovereign capabilities (A197)."""

    ok: bool
    declared_capabilities: tuple[str, ...]
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ===========================================================================
# A198/E172 — Local code and third-party dependency boundary
# ===========================================================================

ALLOWED_STACK: Final[tuple[str, ...]] = (
    "python",
    "typescript",
    "cpp",
    "c",
    "csharp",
    "sql",
)

# A198: FORBIDDEN — dependency boundary violations
DEPENDENCY_BOUNDARY_FORBIDDEN: Final[tuple[str, ...]] = (
    "unmanaged-binary",
    "implicit-download",
    "floating-version",
    "runtime-self-install",
    "package-postinstall-unreviewed-execution",
    "dependency-confused-name",
    "dependency-confused-source",
    "local-code-misclassified-third-party",
    "third-party-code-as-governance-source",
    "license-notice-removal",
    "direct-external-service",
    "legacy-sub-sovereign-owner",
)


@dataclass(frozen=True)
class DependencyBoundaryCheck:
    """Result of verifying local code vs third-party boundary (A198)."""

    ok: bool
    is_local_code: bool
    has_active_decision: bool
    has_exact_identity: bool
    has_locked_version: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ===========================================================================
# A199/E173 — Ten-second complete startup deadline
# ===========================================================================

STARTUP_HARD_DEADLINE_MS: Final[int] = 10000

# A199: BUDGETS — phase budgets in milliseconds
STARTUP_PHASE_BUDGETS_MS: Final[dict[str, int]] = {
    "entry-and-UI": 1000,
    "codex-information-permission": 2000,
    "dependency-DAG-and-services": 4000,
    "sovereign-activation-and-runtime-handoff": 2000,
    "UI-convergence-and-final-proof": 1000,
}

# A199: ALL-FLOWS — all flows that must complete within the deadline
STARTUP_REQUIRED_FLOWS: Final[tuple[str, ...]] = (
    "entry-single-instance-resolution",
    "UI-host-visible",
    "startup-runtime-attach-start",
    "preflight",
    "minimal-information-layer",
    "official-codex-integrity",
    "permission-directory",
    "permission-sovereign",
    "normal-information-mode",
    "certified-dependency-DAG",
    "all-manifest-declared-startup-dependencies",
    "required-sovereigns",
    "runtime-handoff",
    "health-baseline",
    "frontend-backend-snapshot-cursor-convergence",
)

# A199: FORBIDDEN — deadline evasion tactics
STARTUP_DEADLINE_FORBIDDEN: Final[tuple[str, ...]] = (
    "startup-exceeds-10000ms",
    "soft-deadline",
    "deadline-reset-within-generation",
    "wall-clock-only",
    "hidden-wait",
    "infinite-retry",
    "fixed-sleep",
    "serial-independent-work",
    "process-existence-as-ready",
    "stale-cache-as-live-proof",
    "fully-ready-before-all-declared-startup-flows",
    "background-completion-after-ready",
    "optional-deferred-lazy-renaming-to-hide-required-work",
    "UI-blocked-or-closed-on-timeout",
    "independent-tool-stop",
    "manual-click-approval-refresh-to-complete",
    "average-only-compliance",
    "timeout-without-exact-node",
)
