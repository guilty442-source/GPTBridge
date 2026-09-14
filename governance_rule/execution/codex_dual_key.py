"""Dual-key authorization for privileged official-entry operations (A174).

法典依據:
- A174: the official entry is owned by the permission-sovereign; privileged
  review / amendment operations must not proceed on a single actor's word.
- A313-style two-key boundary: one key requests, a distinct registered
  sovereign key countersigns.  A missing or stale countersignature fails
  closed — the operation simply does not open.

A dual-key grant is a single-use, expiry-bound, version-bound token minted
through ``mint_dual_key_grant`` and consumed by the official entry through
``verify_dual_key_grant``.  Grant records persist in the governed entry
state store, so replay protection survives restarts; every mint, consume
and denial emits a metadata-only audit record (no codex content, A174).

Privileged triggers (``requires_dual_key``): ``codex:full`` snapshots and
the ``amendment-verification`` purpose.  星澄's Chinese review is exempt —
its A173 access class is already identity-bound to 星澄 alone.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Iterable

from governance_rule.execution import codex_entry_state as _state
from governance_rule.execution.codex_repository import load_governance_codex

DEFAULT_GRANT_TTL: float = 120.0


def requires_dual_key(
    access_class: str, purpose: str, scope: frozenset[str]
) -> bool:
    """Whether opening this entry requires a dual-key grant."""
    if access_class == _state.ACCESS_CHINESE:
        return False  # A173: identity-bound to 星澄, single key by law.
    return purpose == "amendment-verification" or "codex:full" in scope


def _sovereign_ids() -> frozenset[str]:
    try:
        return frozenset(
            str(s.id) for s in load_governance_codex().sovereigns
        )
    except (OSError, ValueError, KeyError, RuntimeError, ImportError, AttributeError):
        return frozenset()


def _primary_eligible(actor: str, access_class: str) -> bool:
    sovereign_ids = _sovereign_ids()
    if not sovereign_ids:
        return False  # codex unreadable -> fail closed
    if access_class == _state.ACCESS_REVIEW:
        return (
            actor in sovereign_ids
            or actor in _state.REVIEW_COMPONENT_ACTORS
        )
    return (
        actor in sovereign_ids
        or actor in _state.COMPONENT_ACTORS
        or actor in _state.REVIEW_COMPONENT_ACTORS
        or actor in _state.XINGCHENG_IDS
    )


def _countersigner_eligible(actor: str) -> bool:
    """The second key must be a registered sovereign identity."""
    return actor in _sovereign_ids() or actor in _state.XINGCHENG_IDS


def _audit(result: str, *, primary: str, secondary: str, purpose: str,
           scope: frozenset[str], correlation: str) -> None:
    _state.record_session_audit(
        event="dual-key",
        actor=primary,
        purpose=purpose,
        access_class="dual-key",
        scope=scope,
        codex_version=None,
        correlation=correlation,
        result=f"{result}:secondary={secondary}",
    )


def mint_dual_key_grant(
    *,
    operation: str,
    primary_actor: str,
    secondary_actor: str,
    purpose: str,
    scope: Iterable[str],
    access_class: str = _state.ACCESS_REVIEW,
    ttl_seconds: float = DEFAULT_GRANT_TTL,
) -> str:
    """Mint a single-use dual-key grant; returns the grant nonce.

    Both keys are validated independently: the primary must be eligible
    for the requested access class and the countersigner must be a
    distinct registered sovereign identity.  The grant binds to the
    operation, actors, purpose, scope hash, codex version and revocation
    generation; any later mismatch fails closed.
    """
    primary = str(primary_actor or "").strip()
    secondary = str(secondary_actor or "").strip()
    operation = str(operation or "").strip()
    purpose = str(purpose or "").strip()
    try:
        parsed_scope = _state.parse_scope(scope)
    except PermissionError:
        _audit(
            "DENIED_SCOPE", primary=primary, secondary=secondary,
            purpose=purpose, scope=frozenset(), correlation="",
        )
        raise
    denial = None
    if not operation:
        denial = "CODEX_OPERATION_REQUIRED"
    elif not primary or not secondary:
        denial = "CODEX_DUAL_KEY_REQUIRED"
    elif primary == secondary:
        denial = "CODEX_DUAL_KEY_DUPLICATE"
    elif purpose not in _state.GOVERNED_PURPOSES:
        denial = "CODEX_PURPOSE_REQUIRED"
    elif not _primary_eligible(primary, access_class):
        denial = "CODEX_PRIMARY_DENIED"
    elif not _countersigner_eligible(secondary):
        denial = "CODEX_SECONDARY_DENIED"
    if denial is not None:
        _audit(
            denial, primary=primary, secondary=secondary,
            purpose=purpose, scope=parsed_scope, correlation="",
        )
        raise PermissionError(denial)

    nonce = secrets.token_hex(16)
    record = {
        "operation": operation,
        "primary": primary,
        "secondary": secondary,
        "purpose": purpose,
        "access_class": access_class,
        "scope_hash": _state.scope_hash(parsed_scope),
        "codex_version": int(load_governance_codex().codex_version),
        "generation": int(_state.current_revocation()),
        "expires_at": time.time() + max(1.0, float(ttl_seconds)),
        "consumed": False,
    }

    def _mint(state: dict[str, Any]) -> None:
        if nonce in state["grants"] or nonce in state["consumed_nonces"]:
            raise PermissionError("CODEX_NONCE_REPLAY")
        state["grants"][nonce] = record

    _state.mutate_entry_state(_mint)
    _audit(
        "MINTED", primary=primary, secondary=secondary,
        purpose=purpose, scope=parsed_scope, correlation=nonce,
    )
    return nonce


def verify_dual_key_grant(
    grant_id: str,
    *,
    operation: str,
    actor: str,
    purpose: str,
    scope: Iterable[str],
    access_class: str,
) -> None:
    """Consume a dual-key grant; raises PermissionError on any mismatch.

    Single-use: a consumed, expired, revoked, wrong-purpose, wrong-scope,
    wrong-version or wrong-operation grant is denied — and an unknown or
    already-consumed nonce is a replay and is denied.
    """
    nonce = str(grant_id or "").strip()
    parsed_scope = _state.parse_scope(scope)
    actor = str(actor or "").strip()
    purpose = str(purpose or "").strip()
    denial: str | None = None
    record: dict[str, Any] | None = None

    def _consume(state: dict[str, Any]) -> None:
        nonlocal denial, record
        record = state["grants"].get(nonce)
        if nonce in state["consumed_nonces"]:
            denial = "CODEX_GRANT_REPLAY"
        elif record is None:
            denial = "CODEX_GRANT_UNKNOWN"
        elif record.get("consumed"):
            denial = "CODEX_GRANT_REPLAY"
        elif record.get("generation") != state["revocation_generation"]:
            denial = "CODEX_GRANT_REVOKED"
        elif float(record.get("expires_at", 0.0)) < time.time():
            denial = "CODEX_GRANT_EXPIRED"
        else:
            record["consumed"] = True
            state["consumed_nonces"][nonce] = _state.utc_now()

    _state.mutate_entry_state(_consume)
    secondary = str((record or {}).get("secondary", ""))
    if denial is None:
        if record.get("operation") != operation:
            denial = "CODEX_GRANT_OPERATION_MISMATCH"
        elif record.get("primary") != actor:
            denial = "CODEX_GRANT_ACTOR_MISMATCH"
        elif record.get("purpose") != purpose:
            denial = "CODEX_GRANT_PURPOSE_MISMATCH"
        elif record.get("scope_hash") != _state.scope_hash(parsed_scope):
            denial = "CODEX_GRANT_SCOPE_MISMATCH"
        elif record.get("access_class") != access_class:
            denial = "CODEX_GRANT_CLASS_MISMATCH"
        else:
            try:
                current_version = load_governance_codex().codex_version
            except (OSError, ValueError, KeyError, RuntimeError, ImportError, AttributeError):
                current_version = None
            if record.get("codex_version") != current_version:
                denial = "CODEX_VERSION_CHANGED"
    if denial is not None:
        _audit(
            denial, primary=actor, secondary=secondary,
            purpose=purpose, scope=parsed_scope, correlation=nonce,
        )
        raise PermissionError(denial)
    _audit(
        "CONSUMED", primary=actor, secondary=secondary,
        purpose=purpose, scope=parsed_scope, correlation=nonce,
    )


__all__ = [
    "DEFAULT_GRANT_TTL",
    "mint_dual_key_grant",
    "requires_dual_key",
    "verify_dual_key_grant",
]
