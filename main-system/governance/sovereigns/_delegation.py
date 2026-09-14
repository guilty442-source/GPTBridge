"""Single-use in-process delegation sessions with audit, generation ID,
and process boundary proof (A121/A174).

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
delegation session is rejected (A121/A174 fail-closed).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final


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
    """Mint a single-use delegation session nonce (A174 single-use).

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


__all__ = ["consume_delegation", "mint_delegation", "record_delegation_outcome"]


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
    """Record the outcome of a sovereign's delegation step (A69/A121).

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
        "basis": list(basis),
        "process_id": _process_id,
        "timestamp": _iso_now(),
    })
