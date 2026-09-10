"""View access types and constants — A186/E161.

Information-layer sublayer definitions, view level constants, and dataclasses
for view access grants.  This module has no imports from verification or
signal submodules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
# Field policy result (A186: FIELD-POLICY)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldPolicyResult:
    """Result of applying field policy to a set of fields (A186: FIELD-POLICY)."""

    allowed: tuple[str, ...]
    redacted: tuple[str, ...]
    omitted: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
