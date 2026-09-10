"""View access verification functions — A186/E161.

Effective level calculation, field policy enforcement, and sublayer pipeline
verification for the layered information system.
"""

from __future__ import annotations

from typing import Any

from core_system.view_access_types import (
    ACTOR_CEILINGS,
    FieldPolicyResult,
    INFORMATION_LAYER_SUBLAYERS,
    VIEW_LEVEL_DEFAULT,
    VIEW_LEVELS,
    ViewGrant,
)


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
