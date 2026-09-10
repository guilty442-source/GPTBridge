"""Third-party sovereign, dependency boundary, and startup deadline — A197-E173.

This module implements three provisions from codex v2.81330:

  * **A197/E171** — third-party-sovereign-optimized-responsibility (max 3
    capabilities: identity-inventory-provenance, license-security-compatibility-
    risk, admission-version-lifecycle-decision).
  * **A198/E172** — local-code-and-third-party-dependency-boundary (local
    owned source vs immutable vendored/package-managed artifacts).
  * **A199/E173** — ten-second-complete-startup-deadline (10000ms monotonic
    hard deadline from entry-accepted to fully-ready).

All functions are **read-only verification and signal production**.  They
never download, install, mutate, activate, or bypass the information layer.
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


def verify_third_party_capabilities(
    declared: tuple[str, ...],
) -> ThirdPartyCapabilityCheck:
    """Verify third-party sovereign has at most 3 capabilities (A197/E171).

    Per A197: ``MAX-CAPABILITIES:3`` and ``FORBID:third-party-sovereign-direct-
    download/install/uninstall/update/downgrade/replace/delete/execute/activate/
    rollback+manifest/lockfile/source/runtime/data mutation``.
    """
    violations: list[str] = []
    if len(declared) > MAX_THIRD_PARTY_CAPABILITIES:
        violations.append(
            f"exceeds-max-capabilities:{len(declared)}>{MAX_THIRD_PARTY_CAPABILITIES}"
        )
    for cap in declared:
        if cap in THIRD_PARTY_DOES_NOT_DECIDE:
            violations.append(f"forbidden-capability:{cap}")
    return ThirdPartyCapabilityCheck(
        ok=len(violations) == 0,
        declared_capabilities=declared,
        violations=tuple(violations),
    )


def verify_third_party_decision(
    decision: ThirdPartyDecision,
) -> dict[str, Any]:
    """Verify a third-party admission decision (A197/E171).

    Per A197: ``DECISION-BOUNDARY:decides whether exact third-party artifact
    is admissible and which version constraint is acceptable+does-not-decide
    system-priority/release-acceptance/permission/runtime-activation/
    maintenance-health/code-change``.
    """
    violations: list[str] = []
    if not decision.valid_decision_type:
        violations.append(f"invalid-decision-type:{decision.decision}")
    if not decision.dependency_id:
        violations.append("missing-dependency-id")
    if not decision.version:
        violations.append("missing-version")
    return {
        "ok": len(violations) == 0,
        "basis": "A197/E171",
        "decision": decision.as_dict(),
        "violations": violations,
        "automatic_update": False,
        "probe_registered": True,
    }


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


def verify_dependency_boundary(
    *,
    is_local_code: bool,
    has_active_a197_decision: bool,
    has_exact_content_identity: bool,
    has_locked_version: bool,
    has_declared_owner: bool,
    has_license_evidence: bool,
    has_security_evidence: bool,
    has_provenance_evidence: bool,
    has_compatibility_evidence: bool,
) -> DependencyBoundaryCheck:
    """Verify local code vs third-party dependency boundary (A198/E172).

    Per A198: ``DEPENDENCY-ENTRY:requires-active-A197-decision+exact-content-
    identity+locked-version+declared-owner/purpose+license/security/provenance/
    compatibility evidence``.
    """
    violations: list[str] = []

    if not is_local_code:
        # Third-party dependency — must have all evidence
        if not has_active_a197_decision:
            violations.append("missing-active-A197-decision")
        if not has_exact_content_identity:
            violations.append("missing-exact-content-identity")
        if not has_locked_version:
            violations.append("missing-locked-version")
        if not has_declared_owner:
            violations.append("missing-declared-owner")
        if not has_license_evidence:
            violations.append("missing-license-evidence")
        if not has_security_evidence:
            violations.append("missing-security-evidence")
        if not has_provenance_evidence:
            violations.append("missing-provenance-evidence")
        if not has_compatibility_evidence:
            violations.append("missing-compatibility-evidence")

    return DependencyBoundaryCheck(
        ok=len(violations) == 0,
        is_local_code=is_local_code,
        has_active_decision=has_active_a197_decision,
        has_exact_identity=has_exact_content_identity,
        has_locked_version=has_locked_version,
        violations=tuple(violations),
    )


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


@dataclass(frozen=True)
class StartupDeadlineCheck:
    """Result of verifying the 10-second startup deadline (A199).

    Per A199: ``HARD-DEADLINE:10000ms inclusive`` and ``AT-10000MS:not-fully-
    ready=>atomically-mark-generation-failed+cancel-unfinished-owned-startup-
    work+reverse-DAG-cleanup+preserve-running-independent-tools+keep-UI-visible``.
    """

    ok: bool
    elapsed_ms: int
    fully_ready: bool
    completed_flows: tuple[str, ...]
    missing_flows: tuple[str, ...]
    phase_timings: dict[str, int]
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_startup_deadline(
    *,
    elapsed_ms: int,
    fully_ready: bool,
    completed_flows: tuple[str, ...],
    phase_timings: dict[str, int] | None = None,
) -> StartupDeadlineCheck:
    """Verify the 10-second startup deadline (A199/E173).

    Per A199: ``HARD-DEADLINE:10000ms inclusive`` and ``ACCEPTANCE:p50/p95/p99
    and-every-observed-startup individually<=10000ms+no-average-masking``.
    """
    violations: list[str] = []
    timings = phase_timings or {}

    # Check hard deadline
    if elapsed_ms > STARTUP_HARD_DEADLINE_MS:
        violations.append(f"exceeds-hard-deadline:{elapsed_ms}>{STARTUP_HARD_DEADLINE_MS}")

    # Check fully-ready requires all flows complete
    missing = tuple(
        flow for flow in STARTUP_REQUIRED_FLOWS
        if flow not in completed_flows
    )
    if fully_ready and missing:
        violations.append(f"fully-ready-with-missing-flows:{missing}")

    if not fully_ready and elapsed_ms <= STARTUP_HARD_DEADLINE_MS:
        # Not ready but within deadline — not a violation, just not ready
        pass

    # Check phase budgets (informational; carry allowed)
    total_budget = sum(timings.values())
    if total_budget > STARTUP_HARD_DEADLINE_MS:
        violations.append(
            f"phase-total-exceeds-deadline:{total_budget}>{STARTUP_HARD_DEADLINE_MS}"
        )

    return StartupDeadlineCheck(
        ok=len(violations) == 0,
        elapsed_ms=elapsed_ms,
        fully_ready=fully_ready,
        completed_flows=completed_flows,
        missing_flows=missing,
        phase_timings=timings,
        violations=tuple(violations),
    )


def startup_deadline_signal(
    *,
    elapsed_ms: int,
    failed_node: str = "",
    generation_id: str = "",
) -> dict[str, Any]:
    """Produce a signal for startup deadline failure (A199: AT-10000MS).

    Per A199: ``AT-10000MS:not-fully-ready=>atomically-mark-generation-failed+
    cancel-unfinished-owned-startup-work+reverse-DAG-cleanup+preserve-running-
    independent-tools+keep-UI-visible+publish phase/node/elapsed/remaining/
    timeout/evidence/remediation``.
    """
    return {
        "signal_type": "startup-deadline-exceeded",
        "authority": "signal-only",
        "basis": "A199/E173",
        "elapsed_ms": elapsed_ms,
        "deadline_ms": STARTUP_HARD_DEADLINE_MS,
        "failed_node": failed_node,
        "generation_id": generation_id,
        "action": (
            "mark-generation-failed+cancel-unfinished-owned-startup-work+"
            "reverse-DAG-cleanup+preserve-running-independent-tools+"
            "keep-UI-visible+publish-exact-bottleneck"
        ),
        "retry": "automatic-new-generation-after-bounded-backoff",
        "deadline_reset_within_generation": False,
        "manual_action_required": False,
        "false_ready": False,
    }


__all__ = [
    "ALLOWED_STACK",
    "DEPENDENCY_BOUNDARY_FORBIDDEN",
    "DependencyBoundaryCheck",
    "DependencyIdentity",
    "MAX_THIRD_PARTY_CAPABILITIES",
    "STARTUP_DEADLINE_FORBIDDEN",
    "STARTUP_HARD_DEADLINE_MS",
    "STARTUP_PHASE_BUDGETS_MS",
    "STARTUP_REQUIRED_FLOWS",
    "StartupDeadlineCheck",
    "THIRD_PARTY_DECIDES",
    "THIRD_PARTY_DECISION_TYPES",
    "THIRD_PARTY_DOES_NOT_DECIDE",
    "THIRD_PARTY_FORBIDDEN_ACTIONS",
    "THIRD_PARTY_SCOPE",
    "THIRD_PARTY_SOVEREIGN_CAPABILITIES",
    "ThirdPartyCapabilityCheck",
    "ThirdPartyDecision",
    "startup_deadline_signal",
    "verify_dependency_boundary",
    "verify_startup_deadline",
    "verify_third_party_capabilities",
    "verify_third_party_decision",
]
