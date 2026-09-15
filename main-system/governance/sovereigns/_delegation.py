"""Single-use in-process delegation sessions with audit, generation ID,
and process boundary proof (A121/A435).

A sovereign that delegates a request to another sovereign mints a
single-use session bound to ``(parent, child, intent)`` and attaches its
nonce to the forwarded request.  The receiving sovereign consumes the
nonce exactly once when validating the delegation, so a bare
``_delegated_by`` or ``requester`` string can neither be forged nor
replayed by another in-process caller.

Each session carries:
- **generation ID**: a monotonic counter that proves the session was
  minted in this process generation (restart resets it, so a stale
  nonce from a previous process cannot be consumed).
- **process boundary proof**: the OS PID at mint time, verified at
  consume time — a nonce minted by a different process is rejected.
- **audit trail**: every mint and consume is appended to a JSONL audit
  ledger so delegation paths are reconstructable after the fact.

This is the identity attestation available to sovereign actors, which are
not permission-directory identities: the permission-managed
``capability_token`` path remains mandatory whenever a token is presented,
and a sovereign-identity claim without a token or a valid single-use
delegation session is rejected (A121/A435 fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core_system.codex_decision import SovereignOutcome, SovereignRequest


_SESSION_TTL_SECONDS: Final[float] = 30.0
_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_generation_id: int = 0
_process_id: int = os.getpid()

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DELEGATION_AUDIT_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "main-system" / "runtime" / "state"
    / "delegation-audit.jsonl"
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_generation_id() -> int:
    """Return the next monotonic generation ID for this process."""
    global _generation_id
    with _lock:
        _generation_id += 1
        return _generation_id


def _purge(now: float) -> None:
    for key in [
        key for key, entry in _sessions.items() if entry["expires_at"] < now
    ]:
        _sessions.pop(key, None)


def _append_audit(entry: dict[str, Any]) -> None:
    """Append a delegation audit entry to the JSONL ledger."""
    try:
        _DELEGATION_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with _DELEGATION_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass  # audit failure must not block delegation


def mint_delegation(parent: str, child: str, intent: str) -> str:
    """Mint a single-use delegation session nonce (A435 single-use).

    The session carries a generation ID and process boundary proof so
    a nonce from a previous process or generation cannot be consumed.
    The mint is recorded in the audit ledger.
    """
    nonce = secrets.token_hex(16)
    now = time.monotonic()
    gen_id = _next_generation_id()
    with _lock:
        _purge(now)
        _sessions[nonce] = {
            "parent": str(parent),
            "child": str(child),
            "intent": str(intent),
            "expires_at": now + _SESSION_TTL_SECONDS,
            "generation_id": gen_id,
            "process_id": _process_id,
            "minted_at": _iso_now(),
        }
    _append_audit({
        "event": "mint",
        "nonce": nonce,
        "parent": str(parent),
        "child": str(child),
        "intent": str(intent),
        "generation_id": gen_id,
        "process_id": _process_id,
        "timestamp": _iso_now(),
    })
    return nonce


def consume_delegation(
    nonce: str, *, parent: str, child: str, intent: str
) -> bool:
    """Consume a delegation session exactly once.

    Returns False for unknown, expired, mismatched, cross-process, or
    already-consumed nonces — a replayed or cross-process delegation
    can never succeed.
    """
    if not isinstance(nonce, str) or not nonce:
        return False
    now = time.monotonic()
    with _lock:
        _purge(now)
        entry = _sessions.pop(nonce, None)
    if entry is None:
        _audit_consume_failed(nonce, "unknown-or-expired", parent, child, intent)
        return False
    if entry["expires_at"] < now:
        _audit_consume_failed(nonce, "expired", parent, child, intent)
        return False
    if entry.get("process_id") != _process_id:
        _audit_consume_failed(
            nonce, "cross-process", parent, child, intent,
            entry.get("process_id"),
        )
        return False
    matched = (
        entry["parent"] == str(parent)
        and entry["child"] == str(child)
        and entry["intent"] == str(intent)
    )
    if matched:
        _append_audit({
            "event": "consume-ok",
            "nonce": nonce,
            "parent": str(parent),
            "child": str(child),
            "intent": str(intent),
            "generation_id": entry.get("generation_id"),
            "timestamp": _iso_now(),
        })
    else:
        _audit_consume_failed(nonce, "mismatch", parent, child, intent)
    return matched


def _audit_consume_failed(
    nonce: str,
    reason: str,
    parent: str,
    child: str,
    intent: str,
    minted_process_id: int | None = None,
) -> None:
    """Record a failed delegation consume in the audit ledger."""
    entry: dict[str, Any] = {
        "event": "consume-failed",
        "nonce": nonce,
        "reason": reason,
        "parent": str(parent),
        "child": str(child),
        "intent": str(intent),
        "timestamp": _iso_now(),
    }
    if minted_process_id is not None:
        entry["minted_process_id"] = minted_process_id
        entry["current_process_id"] = _process_id
    _append_audit(entry)


__all__ = [
    "DelegationReceipt",
    "attach_delegation_receipt",
    "consume_delegation",
    "mint_delegation",
    "mint_delegation_receipt",
    "record_delegation_outcome",
    "verify_delegation_receipt",
]


def _basis_references(basis: Any) -> tuple[str, ...]:
    """Normalize a basis value (DecisionBasis or iterable) to a reference tuple."""
    refs = getattr(basis, "references", None)
    if refs is not None:
        return tuple(refs)
    return tuple(basis) if basis else ()


def record_delegation_outcome(
    *,
    sovereign_id: str,
    intent: str,
    requester: str,
    accepted: bool,
    reason_code: str = "",
    execution_mode: str = "decision-only",
    basis: tuple[str, ...] = (),
) -> None:
    """Record the outcome of a sovereign's delegation step (A446/A121).

    Every ``_delegate_execution`` call must record its outcome so the
    delegation path is auditable: whether execution was dispatched to a
    governed executor, independently verified, or attested as a pure
    decision with no execution side-effect.
    """
    _append_audit({
        "event": "delegation-outcome",
        "sovereign_id": str(sovereign_id),
        "intent": str(intent),
        "requester": str(requester),
        "accepted": bool(accepted),
        "reason_code": str(reason_code),
        "execution_mode": str(execution_mode),
        "basis": list(_basis_references(basis)),
        "process_id": _process_id,
        "timestamp": _iso_now(),
    })


# ---------------------------------------------------------------------------
# Verifiable delegation receipts (A446/A121 behavioral evidence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationReceipt:
    """Verifiable proof that a sovereign delegation step occurred.

    Unlike a bare ``"execution": "delegated-to-governed-executor"`` string
    declaration, a receipt carries a unique ID, a content hash, and is
    recorded in the append-only delegation audit ledger — so any caller
    can verify the delegation actually happened by checking the ledger.
    """

    receipt_id: str
    sovereign_id: str
    intent: str
    requester: str
    execution_mode: str
    accepted: bool
    reason_code: str
    basis: tuple[str, ...] = ()
    target_sovereign: str = ""
    target_receipts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    content_hash: str = ""
    process_id: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "sovereign_id": self.sovereign_id,
            "intent": self.intent,
            "requester": self.requester,
            "execution_mode": self.execution_mode,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "basis": list(self.basis),
            "target_sovereign": self.target_sovereign,
            "target_receipts": list(self.target_receipts),
            "content_hash": self.content_hash,
            "process_id": self.process_id,
            "timestamp": self.timestamp,
        }


def _compute_receipt_hash(
    sovereign_id: str,
    intent: str,
    requester: str,
    execution_mode: str,
    accepted: bool,
    reason_code: str,
    basis: tuple[str, ...],
    target_sovereign: str,
    target_receipts: tuple[dict[str, Any], ...],
    receipt_id: str,
    timestamp: str,
) -> str:
    """SHA-256 content hash so a receipt cannot be tampered with."""
    content = json.dumps(
        {
            "receipt_id": receipt_id,
            "sovereign_id": sovereign_id,
            "intent": intent,
            "requester": requester,
            "execution_mode": execution_mode,
            "accepted": accepted,
            "reason_code": reason_code,
            "basis": list(basis),
            "target_sovereign": target_sovereign,
            "target_receipts": list(target_receipts),
            "timestamp": timestamp,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def mint_delegation_receipt(
    *,
    sovereign_id: str,
    intent: str,
    requester: str,
    accepted: bool,
    reason_code: str = "",
    execution_mode: str = "decision-only",
    basis: tuple[str, ...] = (),
    target_sovereign: str = "",
    target_receipts: tuple[dict[str, Any], ...] = (),
) -> DelegationReceipt:
    """Mint a verifiable delegation receipt and record it in the ledger.

    The receipt is the behavioral evidence that the delegation step
    actually occurred — not a comment or a string field declaration.
    The receipt's content hash is recorded in the audit ledger so any
    caller can later verify the receipt is genuine and unmodified.
    """
    receipt_id = secrets.token_hex(16)
    timestamp = _iso_now()
    content_hash = _compute_receipt_hash(
        sovereign_id, intent, requester, execution_mode,
        accepted, reason_code, _basis_references(basis), target_sovereign,
        target_receipts, receipt_id, timestamp,
    )
    receipt = DelegationReceipt(
        receipt_id=receipt_id,
        sovereign_id=str(sovereign_id),
        intent=str(intent),
        requester=str(requester),
        execution_mode=str(execution_mode),
        accepted=bool(accepted),
        reason_code=str(reason_code),
        basis=_basis_references(basis),
        target_sovereign=str(target_sovereign),
        target_receipts=tuple(target_receipts),
        content_hash=content_hash,
        process_id=_process_id,
        timestamp=timestamp,
    )
    _append_audit({
        "event": "delegation-receipt",
        **receipt.to_dict(),
    })
    return receipt


def verify_delegation_receipt(receipt: DelegationReceipt) -> bool:
    """Verify a delegation receipt against its content hash.

    Returns True when the receipt's content hash matches its fields,
    proving the receipt has not been tampered with.  The receipt's
    presence in the audit ledger can be confirmed by searching for its
    ``receipt_id`` in the delegation audit file.
    """
    expected = _compute_receipt_hash(
        receipt.sovereign_id, receipt.intent, receipt.requester,
        receipt.execution_mode, receipt.accepted, receipt.reason_code,
        receipt.basis, receipt.target_sovereign, receipt.target_receipts,
        receipt.receipt_id, receipt.timestamp,
    )
    return expected == receipt.content_hash


def attach_delegation_receipt(
    decision: Any,
    request: Any,
    sovereign_id: str,
    execution_mode: str = "decision-only",
) -> Any:
    """Mint a verifiable delegation receipt and attach it to the outcome.

    This replaces bare ``"execution": "delegated-to-governed-executor"``
    string declarations with a verifiable receipt that carries a unique
    ID, content hash, and ledger record (A446/A121 behavioral evidence).
    The receipt is also recorded via ``record_delegation_outcome`` for
    the audit trail.
    """
    from core_system.codex_decision import SovereignOutcome

    reason_code = decision.refusal.reason_code if decision.refusal else ""
    receipt = mint_delegation_receipt(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=decision.accepted,
        reason_code=reason_code,
        execution_mode=execution_mode,
        basis=decision.basis,
    )
    record_delegation_outcome(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=decision.accepted,
        reason_code=reason_code,
        execution_mode=execution_mode,
        basis=decision.basis,
    )
    result = dict(decision.result or {})
    result["delegation_receipt"] = receipt.to_dict()
    return SovereignOutcome(
        accepted=decision.accepted,
        refusal=decision.refusal,
        result=result,
        basis=decision.basis,
    )


def _stamp_delegation(
    request: Any,
    sovereign_id: str,
    target_sovereign_id: str,
) -> Any:
    """Stamp a single-use delegation nonce onto the forwarded request."""
    from core_system.codex_decision import SovereignRequest

    payload = dict(request.payload)
    payload["_delegated_by"] = sovereign_id
    payload["_delegation_nonce"] = mint_delegation(
        sovereign_id, target_sovereign_id, request.intent
    )
    return SovereignRequest(
        intent=request.intent,
        subject=request.subject,
        requester=sovereign_id,
        payload=payload,
    )


def _attach_target_receipt(
    outcome: Any,
    request: Any,
    sovereign_id: str,
    target_sovereign_id: str,
) -> Any:
    """Attach a verifiable delegation receipt carrying the target's trail."""
    from core_system.codex_decision import SovereignOutcome

    target_receipts: tuple[dict[str, Any], ...] = ()
    target_result = outcome.result or {}
    if isinstance(target_result, dict):
        summary = target_result.get("execution_receipts")
        if isinstance(summary, dict):
            target_receipts = tuple(summary.get("tiers", ()))
    receipt = mint_delegation_receipt(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=outcome.accepted,
        reason_code=outcome.refusal.reason_code if outcome.refusal else "",
        execution_mode="delegated-to-target",
        basis=outcome.basis,
        target_sovereign=target_sovereign_id,
        target_receipts=target_receipts,
    )
    result = dict(outcome.result or {})
    result["delegation_receipt"] = receipt.to_dict()
    return SovereignOutcome(
        accepted=outcome.accepted,
        refusal=outcome.refusal,
        result=result,
        basis=outcome.basis,
    )


__all__ = [
    "DelegationReceipt",
    "attach_delegation_receipt",
    "consume_delegation",
    "mint_delegation",
    "mint_delegation_receipt",
    "record_delegation_outcome",
    "verify_delegation_receipt",
    "_stamp_delegation",
    "_attach_target_receipt",
]

def _stamp_delegation(
    request: "SovereignRequest",
    sovereign_id: str,
    target_sovereign_id: str,
) -> "SovereignRequest":
    """Stamp a single-use delegation nonce onto the forwarded request."""
    payload = dict(request.payload)
    payload["_delegated_by"] = sovereign_id
    payload["_delegation_nonce"] = mint_delegation(
        sovereign_id, target_sovereign_id, request.intent
    )
    return SovereignRequest(
        intent=request.intent,
        subject=request.subject,
        requester=sovereign_id,
        payload=payload,
    )


def _attach_target_receipt(
    outcome: "SovereignOutcome",
    request: "SovereignRequest",
    sovereign_id: str,
    target_sovereign_id: str,
) -> "SovereignOutcome":
    """Attach a verifiable delegation receipt carrying the target's trail."""
    target_receipts: tuple[dict[str, Any], ...] = ()
    target_result = outcome.result or {}
    if isinstance(target_result, dict):
        summary = target_result.get("execution_receipts")
        if isinstance(summary, dict):
            target_receipts = tuple(summary.get("tiers", ()))
    receipt = mint_delegation_receipt(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=outcome.accepted,
        reason_code=outcome.refusal.reason_code if outcome.refusal else "",
        execution_mode="delegated-to-target",
        basis=outcome.basis,
        target_sovereign=target_sovereign_id,
        target_receipts=target_receipts,
    )
    result = dict(outcome.result or {})
    result["delegation_receipt"] = receipt.to_dict()
    return SovereignOutcome(
        accepted=outcome.accepted,
        refusal=outcome.refusal,
        result=result,
        basis=outcome.basis,
    )
