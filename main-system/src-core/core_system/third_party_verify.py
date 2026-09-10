"""Third-party verification functions — A197/E171, A198/E172, A199/E173.

Verification logic extracted from third_party_governance for source-size
compliance (A185/E160).  All functions are read-only verification.
"""

from __future__ import annotations

from typing import Any

from core_system.third_party_types import (
    MAX_THIRD_PARTY_CAPABILITIES,
    THIRD_PARTY_DOES_NOT_DECIDE,
    DependencyBoundaryCheck,
    ThirdPartyCapabilityCheck,
    ThirdPartyDecision,
)


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
