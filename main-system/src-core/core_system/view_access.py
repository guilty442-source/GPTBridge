"""Layered information system and tiered view access — A186/E161.

Per A186 (layered-information-system-and-tiered-view-access) and E161
(information-layer-view-access), the information layer is a 9-sublayer
pipeline, and view access is governed by tiered levels issued by the
Permission Sovereign.

Information-layer physical sublayers (A186: INFORMATION-LAYER-PHYSICAL-
SUBLAYERS):

  L1 — entry and session
  L2 — identity and attestation
  L3 — permission-proof enforcement
  L4 — contract-type-schema validation
  L5 — scope-field-policy and redaction
  L6 — routing and mediation
  L7 — transport-adapter and connection-lifecycle
  L8 — state-snapshot event-sequence and reconciliation
  L9 — observability audit and health publication

Flow (A186: FLOW): request > L1 > L2 > L3 > L4 > L5 > L6 > L7 > destination >
L7 > L6 > L5 > L8 > L9 > authorized-response.

View levels (A186: VIEW-LEVELS):

  V0 — denied
  V1 — own-identity and liveness
  V2 — own-operational summary
  V3 — owner-scoped detail and nonsecret diagnostics
  V4 — authorized cross-domain diagnostic and redacted audit evidence
  V5 — restricted security/integrity/incident evidence
  VC — authoritative codex view (governed exclusively by A174)

Authority (A186: AUTHORITY): Permission Sovereign decides and issues view
grants.  The information layer validates and enforces proof but does not
grant, expand, or interpret permission.

This module provides **read-only data structures and verification**.  It
never grants permission, mutates grants, or bypasses the Permission
Sovereign's authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

# ---------------------------------------------------------------------------
# Information-layer sublayers (A186: INFORMATION-LAYER-PHYSICAL-SUBLAYERS)
# ---------------------------------------------------------------------------

INFORMATION_LAYER_SUBLAYERS: Final[tuple[str, ...]] = (
    "L1-entry-session",
    "L2-identity-attestation",
    "L3-permission-proof-enforcement",
    "L4-contract-type-schema-validation",
    "L5-scope-field-policy-redaction",
    "L6-routing-mediation",
    "L7-transport-adapter-connection-lifecycle",
    "L8-state-snapshot-event-sequence-reconciliation",
    "L9-observability-audit-health-publication",
)

# A186: FLOW — request through sublayers to destination and back
INFORMATION_LAYER_FLOW: Final[tuple[str, ...]] = (
    "request",
    "L1",
    "L2",
    "L3",
    "L4",
    "L5",
    "L6",
    "L7",
    "destination",
    "L7",
    "L6",
    "L5",
    "L8",
    "L9",
    "authorized-response",
)

# ---------------------------------------------------------------------------
# View levels (A186: VIEW-LEVELS)
# ---------------------------------------------------------------------------

VIEW_LEVELS: Final[tuple[str, ...]] = (
    "V0",  # denied
    "V1",  # own-identity and liveness
    "V2",  # own-operational summary
    "V3",  # owner-scoped detail and nonsecret diagnostics
    "V4",  # authorized cross-domain diagnostic and redacted audit evidence
    "V5",  # restricted security/integrity/incident evidence
    "VC",  # authoritative codex view (A174)
)

VIEW_LEVEL_DEFAULT: Final[str] = "V0"

# A186: ACTOR-CEILINGS — maximum view level per actor class
ACTOR_CEILINGS: Final[dict[str, str]] = {
    "ui": "V2",
    "ui-explicit-detail": "V3",
    "module": "V3",
    "tool": "V3",
    "sub-sovereign": "V3",
    "sovereign": "V4",
    "maintenance": "V4",
    "permission-sovereign": "V5",
    "xingcheng": "V4",
    "xingcheng-codex": "VC",
}

# A186: V5-SECRETS — content that remains non-viewable even at V5
NON_VIEWABLE_SECRETS: Final[tuple[str, ...]] = (
    "private-keys",
    "raw-credentials",
    "tokens",
    "passwords",
    "unencrypted-personal-secret-content",
)


# ---------------------------------------------------------------------------
# View grant (A186: GRANT)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ViewGrant:
    """A Permission Sovereign-issued view access grant (A186: GRANT).

    Per A186: ``GRANT:subject-identity+actor-class+level+owner-scope+
    resource-scope+field-allowlist+purpose+legal-basis+issued-at+expiry+
    session-generation+single-or-bounded-use+decision-proof``.
    """

    subject_identity: str
    actor_class: str
    level: str
    owner_scope: str
    resource_scope: str
    field_allowlist: tuple[str, ...]
    purpose: str
    legal_basis: str
    issued_at: str
    expiry: str
    session_generation: str
    single_use: bool
    decision_proof: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_expired(self) -> bool:
        """Check if the grant has expired (A186: DOWNGRADE: automatic-on-expiry)."""
        if not self.expiry:
            return False
        try:
            expiry_dt = datetime.fromisoformat(self.expiry)
            return datetime.now(timezone.utc) > expiry_dt
        except (ValueError, TypeError):
            return False

    @property
    def is_valid_level(self) -> bool:
        return self.level in VIEW_LEVELS


# ---------------------------------------------------------------------------
# Effective level calculation (A186: EFFECTIVE-LEVEL)
# ---------------------------------------------------------------------------

def effective_view_level(
    requested_level: str,
    grant: ViewGrant | None,
    resource_minimum: str = VIEW_LEVEL_DEFAULT,
    resource_maximum: str = "V5",
    actor_class: str = "",
) -> str:
    """Calculate the effective view level (A186: EFFECTIVE-LEVEL).

    Per A186: ``EFFECTIVE-LEVEL:min(requested-level, grant-level,
    resource-minimum/maximum-policy, actor-ceiling)``.

    If no grant exists, the default is V0 (denied).
    """
    if grant is None or grant.is_expired or not grant.is_valid_level:
        return VIEW_LEVEL_DEFAULT

    ceiling = ACTOR_CEILINGS.get(actor_class or grant.actor_class, "V0")

    levels = VIEW_LEVELS
    try:
        requested_idx = levels.index(requested_level) if requested_level in levels else 0
        grant_idx = levels.index(grant.level) if grant.level in levels else 0
        min_idx = levels.index(resource_minimum) if resource_minimum in levels else 0
        max_idx = levels.index(resource_maximum) if resource_maximum in levels else len(levels) - 1
        ceiling_idx = levels.index(ceiling) if ceiling in levels else 0
    except ValueError:
        return VIEW_LEVEL_DEFAULT

    # Effective = min(requested, grant, max-policy, ceiling) but >= min-policy
    effective_idx = min(requested_idx, grant_idx, max_idx, ceiling_idx)
    effective_idx = max(effective_idx, min_idx)

    return levels[effective_idx]


# ---------------------------------------------------------------------------
# Field policy (A186: FIELD-POLICY)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldPolicyResult:
    """Result of applying field policy to a set of fields (A186: FIELD-POLICY)."""

    allowed: tuple[str, ...]
    redacted: tuple[str, ...]
    omitted: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_field_policy(
    fields: dict[str, Any],
    grant: ViewGrant | None,
    *,
    sensitive_fields: frozenset[str] = frozenset(),
) -> FieldPolicyResult:
    """Apply field policy to a field set (A186: FIELD-POLICY).

    Per A186: ``FIELD-POLICY:allowlist-only+redact-denied-fields+omit-secret-
    material+typed-redaction-marker+no-inference-through-count/error/timing``.

    - Fields in the grant's allowlist are allowed.
    - Fields not in the allowlist but not sensitive are redacted.
    - Sensitive fields (secrets) are omitted entirely.
    """
    if grant is None:
        # No grant: all fields redacted, sensitive omitted.
        allowed: tuple[str, ...] = ()
        redacted = tuple(
            k for k in fields if k not in sensitive_fields
        )
        omitted = tuple(k for k in fields if k in sensitive_fields)
        return FieldPolicyResult(allowed=allowed, redacted=redacted, omitted=omitted)

    allowlist = set(grant.field_allowlist)
    allowed = tuple(k for k in fields if k in allowlist and k not in sensitive_fields)
    redacted = tuple(
        k for k in fields
        if k not in allowlist and k not in sensitive_fields
    )
    omitted = tuple(k for k in fields if k in sensitive_fields)
    return FieldPolicyResult(allowed=allowed, redacted=redacted, omitted=omitted)


# ---------------------------------------------------------------------------
# Sublayer verification (A186: EACH-SUBLAYER)
# ---------------------------------------------------------------------------

def verify_sublayer_pipeline(
    passed_sublayers: tuple[str, ...],
) -> dict[str, Any]:
    """Verify that the sublayer pipeline was followed without skips (A186).

    Per A186: ``EACH-SUBLAYER:one-primary-duty+max-3-capabilities+own-
    interface+no-skip+no-upstream-trust-by-default``.
    """
    required_order = INFORMATION_LAYER_SUBLAYERS
    violations: list[str] = []

    # Check no skip: each required sublayer must appear in order.
    last_idx = -1
    for sublayer in required_order:
        if sublayer not in passed_sublayers:
            violations.append(f"missing-sublayer:{sublayer}")
            continue
        idx = passed_sublayers.index(sublayer)
        if idx <= last_idx:
            violations.append(f"out-of-order:{sublayer}")
        last_idx = idx

    return {
        "ok": len(violations) == 0,
        "basis": "A186/E161",
        "passed_sublayers": list(passed_sublayers),
        "required_sublayers": list(required_order),
        "violations": violations,
        "no_skip": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# View access status (A186: AUDIT)
# ---------------------------------------------------------------------------

def view_access_status(
    grant: ViewGrant | None,
    *,
    effective_level: str = VIEW_LEVEL_DEFAULT,
) -> dict[str, Any]:
    """Return the view access status for audit and observability (A186: AUDIT).

    Per A186: ``AUDIT:requester+actor+level+scope+purpose+fields-category+
    decision+grant-id+time+correlation-id without-protected-content``.
    """
    return {
        "has_grant": grant is not None,
        "grant_level": grant.level if grant else "",
        "effective_level": effective_level,
        "actor_class": grant.actor_class if grant else "",
        "owner_scope": grant.owner_scope if grant else "",
        "purpose": grant.purpose if grant else "",
        "issued_at": grant.issued_at if grant else "",
        "expiry": grant.expiry if grant else "",
        "is_expired": grant.is_expired if grant else False,
        "authority": "permission-sovereign",
        "basis": "A186/E161",
        "default": VIEW_LEVEL_DEFAULT,
    }


__all__ = [
    "ACTOR_CEILINGS",
    "FieldPolicyResult",
    "INFORMATION_LAYER_FLOW",
    "INFORMATION_LAYER_SUBLAYERS",
    "NON_VIEWABLE_SECRETS",
    "VIEW_LEVELS",
    "VIEW_LEVEL_DEFAULT",
    "ViewGrant",
    "apply_field_policy",
    "effective_view_level",
    "verify_sublayer_pipeline",
    "view_access_status",
]
