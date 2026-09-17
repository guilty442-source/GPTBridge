"""Capability verification in the documented order (A390-428 core).

``verify_capability`` walks ``VERIFICATION_ORDER`` exactly::

    parse -> integrity -> expiry -> replay -> repository -> actor ->
    operation -> worktree -> branch -> ref -> revision -> generation ->
    policy -> tier -> consume

Each rejected step returns its 425 failure code and the checks executed so
far; consumption is last, so a token rejected for any binding mismatch is
never burned.  The ledger is the replay authority: a consumed id is
``CAPABILITY_ALREADY_CONSUMED``, a reused nonce is ``CAPABILITY_REPLAY``,
and a different payload under the same id is
``CAPABILITY_MUTATION_FORBIDDEN`` (extend/scope-change/revive attempt).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .capability import (
    HMAC_ALGORITHM,
    UNSIGNED_MARKER,
    VERIFICATION_ORDER,
    CapabilityError,
    CapabilityToken,
    IssuerClass,
    scope_allows,
    system_safe_operation_allowed,
    token_payload_digest,
    verify_token_signature,
)
from .capability_ledger import CapabilityLedger
from .capability_time import as_utc, parse_time

__all__ = [
    "CapabilityContext",
    "VerificationResult",
    "verify_capability",
]


@dataclass(frozen=True)
class CapabilityContext:
    tier: int
    actor: str
    repository_id: str
    operation: str
    command_digest: str
    worktree_id: str = ""
    branch: str = ""
    target_ref: str = ""
    source_revision: str = ""
    target_revision: str = ""
    proposal_generation: int = 0
    policy_version: str = ""
    policy_digest: str = ""

    def resolved(self) -> "CapabilityContext":
        from .capability import policy_digest, policy_version

        return CapabilityContext(
            tier=self.tier, actor=self.actor,
            repository_id=self.repository_id, operation=self.operation,
            command_digest=self.command_digest,
            worktree_id=self.worktree_id, branch=self.branch,
            target_ref=self.target_ref, source_revision=self.source_revision,
            target_revision=self.target_revision,
            proposal_generation=self.proposal_generation,
            policy_version=self.policy_version or policy_version(),
            policy_digest=self.policy_digest or policy_digest(),
        )


@dataclass
class VerificationResult:
    allowed: bool
    code: str
    detail: str = ""
    integrity: str = UNSIGNED_MARKER
    consumed: bool = False
    checks: tuple[str, ...] = ()
    record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _VerifyState:
    now: datetime
    ledger: CapabilityLedger
    require_signature: bool
    integrity: str = UNSIGNED_MARKER
    record: dict[str, Any] = field(default_factory=dict)


def _fail(
    code: str, detail: str, state: _VerifyState, checks: list[str],
) -> VerificationResult:
    return VerificationResult(
        allowed=False, code=code, detail=detail,
        integrity=state.integrity, checks=tuple(checks),
    )


def _check_parse(
    token: Any, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if not isinstance(token, CapabilityToken):
        raise CapabilityError("CAPABILITY_MALFORMED", "not a capability token")
    return None


def _check_integrity(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.signature:
        if not verify_token_signature(token):
            return ("CAPABILITY_INTEGRITY_INVALID", "signature mismatch")
        state.integrity = HMAC_ALGORITHM
        return None
    state.integrity = UNSIGNED_MARKER
    if state.require_signature:
        return ("CAPABILITY_UNSIGNED_REJECTED", UNSIGNED_MARKER)
    return None


def _check_expiry(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    try:
        not_before = parse_time(token.not_before)
        expires_at = parse_time(token.expires_at)
    except ValueError as exc:
        return ("CAPABILITY_MALFORMED", f"timestamp: {exc}")
    if state.now < not_before:
        return ("CAPABILITY_NOT_YET_VALID", token.not_before)
    if state.now >= expires_at:
        return ("CAPABILITY_EXPIRED", token.expires_at)
    return None


def _check_replay(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    digest = token_payload_digest(token)
    for entry in state.ledger.lookup(token.capability_id):
        recorded = str(entry.get("token_digest") or "")
        if recorded and recorded != digest:
            return (
                "CAPABILITY_MUTATION_FORBIDDEN",
                "ledger payload differs (extended/scope-changed/revived)",
            )
    if state.ledger.consumed(token.capability_id):
        return ("CAPABILITY_ALREADY_CONSUMED", token.capability_id)
    owner = state.ledger.nonce_owner(token.nonce)
    if owner and owner != token.capability_id:
        return ("CAPABILITY_REPLAY", f"nonce reused from {owner}")
    return None


def _check_repository(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.repository_id != context.repository_id:
        return ("CAPABILITY_REPOSITORY_MISMATCH", token.repository_id)
    return None


def _check_actor(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.actor != context.actor:
        return ("CAPABILITY_ACTOR_MISMATCH", token.actor)
    return None


def _check_operation(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.operation != context.operation:
        return ("CAPABILITY_OPERATION_MISMATCH", token.operation)
    if token.command_digest != context.command_digest:
        return ("CAPABILITY_COMMAND_MISMATCH", token.command_digest)
    return None


def _check_worktree(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.worktree_id != context.worktree_id:
        return ("CAPABILITY_WORKTREE_MISMATCH", token.worktree_id)
    return None


def _check_branch(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.branch != context.branch:
        return ("CAPABILITY_BRANCH_MISMATCH", token.branch)
    return None


def _check_ref(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.target_ref != context.target_ref:
        return ("CAPABILITY_REF_MISMATCH", token.target_ref)
    return None


def _check_revision(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.source_revision != context.source_revision:
        return ("CAPABILITY_REVISION_MISMATCH", token.source_revision)
    if token.target_revision != context.target_revision:
        return ("CAPABILITY_TARGET_REVISION_MISMATCH", token.target_revision)
    return None


def _check_generation(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.proposal_generation != context.proposal_generation:
        return (
            "CAPABILITY_GENERATION_MISMATCH", str(token.proposal_generation),
        )
    return None


def _check_policy(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if (token.policy_version != context.policy_version
            or token.policy_digest != context.policy_digest):
        return ("CAPABILITY_POLICY_CHANGED", token.policy_digest)
    return None


def _check_tier(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    if token.tier != context.tier:
        return ("CAPABILITY_TIER_FORBIDDEN", str(token.tier))
    klass = token.issuer_class()
    if klass is None:
        return ("CAPABILITY_ISSUER_NOT_AUTHORIZED", token.issuer)
    if token.tier == 3 and klass is not IssuerClass.GOVERNANCE_AUTHORITY:
        return (
            "CAPABILITY_ISSUER_NOT_AUTHORIZED",
            f"tier-3 issuer class {klass.value}",
        )
    if klass is IssuerClass.SYSTEM_SAFE_AUTOMATION and (
        token.tier != 2 or not system_safe_operation_allowed(token.operation)
    ):
        return ("CAPABILITY_ISSUER_NOT_AUTHORIZED", token.operation)
    if not scope_allows(token.scope, token.operation, token.tier):
        return ("CAPABILITY_SCOPE_VIOLATION", ",".join(token.scope))
    return None


def _check_consume(
    token: CapabilityToken, context: CapabilityContext, state: _VerifyState,
) -> tuple[str, str] | None:
    result = "consumed" if token.single_use else "used"
    state.record = state.ledger.record_consume(token, result=result)
    return None


_CHECKS: tuple[tuple[str, Any], ...] = (
    ("parse", _check_parse),
    ("integrity", _check_integrity),
    ("expiry", _check_expiry),
    ("replay", _check_replay),
    ("repository", _check_repository),
    ("actor", _check_actor),
    ("operation", _check_operation),
    ("worktree", _check_worktree),
    ("branch", _check_branch),
    ("ref", _check_ref),
    ("revision", _check_revision),
    ("generation", _check_generation),
    ("policy", _check_policy),
    ("tier", _check_tier),
    ("consume", _check_consume),
)


def verify_capability(
    capability: CapabilityToken | Mapping[str, Any],
    context: CapabilityContext,
    *,
    ledger: CapabilityLedger | None = None,
    now: datetime | float | str | None = None,
    consume: bool = True,
    require_signature: bool = False,
) -> VerificationResult:
    """Verify a capability in the documented order, consuming it last."""
    state = _VerifyState(
        now=as_utc(now),
        ledger=ledger or CapabilityLedger(),
        require_signature=require_signature,
    )
    checks: list[str] = []
    try:
        parsed = (
            capability if isinstance(capability, CapabilityToken)
            else CapabilityToken.from_dict(capability)
        )
    except (ValueError, TypeError) as exc:
        return _fail("CAPABILITY_MALFORMED", str(exc), state, ["parse"])
    working = context.resolved()
    for name, check in _CHECKS:
        if name == "consume" and not consume:
            continue
        checks.append(name)
        try:
            failure = check(parsed, working, state)
        except CapabilityError as exc:
            failure = (exc.code, exc.detail)
        if failure:
            return _fail(failure[0], failure[1], state, checks)
    return VerificationResult(
        allowed=True, code="OK",
        detail="capability verified" + ("" if consume else " (not consumed)"),
        integrity=state.integrity, consumed=consume and parsed.single_use,
        checks=tuple(checks), record=state.record,
    )
