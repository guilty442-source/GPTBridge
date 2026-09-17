"""Capability issuance rules (A390-428 core).

Issuance is where the AI boundary is enforced: an actor can never issue for
itself (``CAPABILITY_SELF_ISSUED``), Tier 3 is restricted to
``IssuerClass.GOVERNANCE_AUTHORITY``, and ``SYSTEM_SAFE_AUTOMATION`` may
issue Tier-2 tokens only for the declared whitelist.  The resulting token is
frozen and signed (HMAC when ``GPTBRIDGE_CAPABILITY_HMAC_KEY`` is set,
otherwise explicitly marked ``UNSIGNED_DEVELOPMENT_CAPABILITY``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Sequence
import uuid

from .capability import (
    CapabilityError,
    CapabilityToken,
    IssuerClass,
    policy_digest,
    policy_version,
    scope_allows,
    sign_token,
    system_safe_operation_allowed,
    token_payload_digest,
    _scope_for,
)
from .capability_time import as_utc, utc_now_iso
from .command_normalizer import command_digest, operation_key

if TYPE_CHECKING:  # pragma: no cover - annotation only (no runtime cycle)
    from .capability_ledger import CapabilityLedger

__all__ = ["issue_capability"]


def _issuer_rule_check(
    issuer_class: IssuerClass, tier: int, operation: str,
) -> None:
    if issuer_class is IssuerClass.GOVERNANCE_AUTHORITY:
        return
    if issuer_class is IssuerClass.SYSTEM_SAFE_AUTOMATION:
        if tier != 2:
            raise CapabilityError(
                "CAPABILITY_ISSUER_NOT_AUTHORIZED",
                "SYSTEM_SAFE_AUTOMATION may not issue tier-3 tokens",
            )
        if not system_safe_operation_allowed(operation):
            raise CapabilityError(
                "CAPABILITY_ISSUER_NOT_AUTHORIZED",
                f"SYSTEM_SAFE_AUTOMATION operation not whitelisted: {operation}",
            )
        return
    if tier == 3:
        raise CapabilityError(
            "CAPABILITY_ISSUER_NOT_AUTHORIZED",
            "tier-3 requires GOVERNANCE_AUTHORITY",
        )


def _self_issue_check(issuer_id: str, actor: str) -> None:
    if str(issuer_id).strip().casefold() == str(actor).strip().casefold():
        raise CapabilityError(
            "CAPABILITY_SELF_ISSUED", "issuer identity equals actor",
        )


def _existing_token_check(
    ledger: "CapabilityLedger | None", capability_id: str, nonce: str,
    digest: str,
) -> None:
    if ledger is None:
        return
    for entry in ledger.lookup(capability_id):
        if str(entry.get("token_digest") or "") not in ("", digest):
            raise CapabilityError(
                "CAPABILITY_MUTATION_FORBIDDEN",
                "capability_id already bound to a different payload",
            )
    owner = ledger.nonce_owner(nonce)
    if owner and owner != capability_id:
        raise CapabilityError(
            "CAPABILITY_REPLAY", f"nonce already issued to {owner}",
        )


def _resolve_issue_binding(
    command: str | Sequence[str] | None, operation: str, digest: str,
    tier: int, scope: Sequence[str] | None,
) -> tuple[str, str, tuple[str, ...]]:
    if command is not None:
        operation = operation_key(command)
        digest = command_digest(command)
    if not operation or not digest:
        raise CapabilityError(
            "CAPABILITY_MALFORMED",
            "operation and command binding are required",
        )
    if tier == 1:
        raise CapabilityError(
            "CAPABILITY_TIER_FORBIDDEN",
            "tier-1 requires no capability token",
        )
    if tier not in (2, 3):
        raise CapabilityError(
            "CAPABILITY_TIER_FORBIDDEN", f"unknown tier {tier}",
        )
    chosen = _scope_for(scope, operation, tier)
    if not scope_allows(chosen, operation, tier):
        raise CapabilityError(
            "CAPABILITY_SCOPE_VIOLATION",
            "scope does not cover operation/tier",
        )
    return operation, digest, chosen


@dataclass(frozen=True)
class _IssueBinding:
    actor: str
    repository_id: str
    operation: str
    command_digest: str
    tier: int
    worktree_id: str
    branch: str
    target_ref: str
    proposal_id: str
    proposal_generation: int
    source_revision: str
    target_revision: str
    single_use: bool
    scope: tuple[str, ...]
    policy_version: str
    policy_digest_value: str


def _build_token(
    binding: _IssueBinding, *, capability_id: str, issuer: str, nonce: str,
    issued_at: str, not_before: str, expires_at: str,
) -> CapabilityToken:
    return CapabilityToken(
        capability_id=capability_id,
        issuer=issuer,
        actor=binding.actor,
        repository_id=binding.repository_id,
        operation=binding.operation,
        tier=binding.tier,
        worktree_id=binding.worktree_id,
        branch=binding.branch,
        target_ref=binding.target_ref,
        command_digest=binding.command_digest,
        proposal_id=binding.proposal_id,
        proposal_generation=binding.proposal_generation,
        source_revision=binding.source_revision,
        target_revision=binding.target_revision,
        issued_at=issued_at,
        not_before=not_before,
        expires_at=expires_at,
        nonce=nonce,
        single_use=binding.single_use,
        policy_version=binding.policy_version,
        policy_digest=binding.policy_digest_value,
        scope=binding.scope,
    )


def issue_capability(
    *,
    issuer_class: IssuerClass | str,
    issuer_id: str,
    actor: str,
    repository_id: str,
    tier: int,
    command: str | Sequence[str] | None = None,
    operation: str = "",
    command_digest_value: str = "",
    scope: Sequence[str] | None = None,
    worktree_id: str = "",
    branch: str = "",
    target_ref: str = "",
    proposal_id: str = "",
    proposal_generation: int = 0,
    source_revision: str = "",
    target_revision: str = "",
    ttl_seconds: float = 300.0,
    not_before: datetime | float | str | None = None,
    issued_at: datetime | float | str | None = None,
    capability_id: str = "",
    nonce: str = "",
    single_use: bool = True,
    policy_version_value: str = "",
    policy_digest_value: str = "",
    key: str | None = None,
    ledger: "CapabilityLedger | None" = None,
) -> CapabilityToken:
    """Issue a scoped capability token (single approval/execution source).

    ``command`` (when given) is the source of ``operation`` and
    ``command_digest``; both approval and execution call the same
    ``command_normalizer`` implementation.
    """
    klass = (
        issuer_class if isinstance(issuer_class, IssuerClass)
        else IssuerClass(str(issuer_class))
    )
    operation, digest, chosen_scope = _resolve_issue_binding(
        command, operation, command_digest_value, tier, scope,
    )
    _self_issue_check(issuer_id, actor)
    _issuer_rule_check(klass, tier, operation)
    issued_dt = as_utc(issued_at)
    not_before_dt = as_utc(not_before) if not_before is not None else issued_dt
    binding = _IssueBinding(
        actor=str(actor), repository_id=str(repository_id),
        operation=operation, command_digest=digest, tier=int(tier),
        worktree_id=str(worktree_id), branch=str(branch),
        target_ref=str(target_ref), proposal_id=str(proposal_id),
        proposal_generation=int(proposal_generation),
        source_revision=str(source_revision),
        target_revision=str(target_revision), single_use=bool(single_use),
        scope=chosen_scope,
        policy_version=policy_version_value or policy_version(),
        policy_digest_value=policy_digest_value or policy_digest(),
    )
    token = _build_token(
        binding,
        capability_id=capability_id or uuid.uuid4().hex,
        issuer=f"{klass.value}:{issuer_id}",
        nonce=nonce or uuid.uuid4().hex,
        issued_at=utc_now_iso(issued_dt),
        not_before=utc_now_iso(not_before_dt),
        expires_at=utc_now_iso(
            issued_dt + timedelta(seconds=float(ttl_seconds))
        ),
    )
    _existing_token_check(
        ledger, token.capability_id, token.nonce, token_payload_digest(token),
    )
    signed = sign_token(token, key=key)
    if ledger is not None:
        ledger.record_issue(signed)
    return signed
