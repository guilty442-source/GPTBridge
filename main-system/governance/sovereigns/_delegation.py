"""Single-use in-process delegation sessions (A121/A174).

A sovereign that delegates a request to another sovereign mints a
single-use session bound to ``(parent, child, intent)`` and attaches its
nonce to the forwarded request.  The receiving sovereign consumes the
nonce exactly once when validating the delegation, so a bare
``_delegated_by`` or ``requester`` string can neither be forged nor
replayed by another in-process caller.

This is the identity attestation available to sovereign actors, which are
not permission-directory identities: the permission-managed
``capability_token`` path remains mandatory whenever a token is presented,
and a sovereign-identity claim without a token or a valid single-use
delegation session is rejected (A121/A174 fail-closed).
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Final


_SESSION_TTL_SECONDS: Final[float] = 30.0
_sessions: dict[str, tuple[str, str, str, float]] = {}
_lock = threading.Lock()


def _purge(now: float) -> None:
    for key in [
        key for key, entry in _sessions.items() if entry[3] < now
    ]:
        _sessions.pop(key, None)


def mint_delegation(parent: str, child: str, intent: str) -> str:
    """Mint a single-use delegation session nonce (A174 single-use)."""
    nonce = secrets.token_hex(16)
    now = time.monotonic()
    with _lock:
        _purge(now)
        _sessions[nonce] = (
            str(parent),
            str(child),
            str(intent),
            now + _SESSION_TTL_SECONDS,
        )
    return nonce


def consume_delegation(
    nonce: str, *, parent: str, child: str, intent: str
) -> bool:
    """Consume a delegation session exactly once.

    Returns False for unknown, expired, mismatched or already-consumed
    nonces — a replayed delegation can never succeed.
    """
    if not isinstance(nonce, str) or not nonce:
        return False
    now = time.monotonic()
    with _lock:
        _purge(now)
        entry = _sessions.pop(nonce, None)
    if entry is None:
        return False
    session_parent, session_child, session_intent, expires_at = entry
    if expires_at < now:
        return False
    return (
        session_parent == str(parent)
        and session_child == str(child)
        and session_intent == str(intent)
    )


__all__ = ["consume_delegation", "mint_delegation"]
