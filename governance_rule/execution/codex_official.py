"""Official codex entry (A74/A173/A174) — the single read-only codex projection.

法典依據:
- A38/A107/A173: the official storage is the single local read-only SQLite
  database; the Chinese language mirror is backup/星澄-only and is never
  the adjudication, citation or machine-governance basis.
- A74: ALL-CODEX-CITATION enters through ``governance-codex://official``
  with an explicit provision id; resolution is read-only.
- A174: sovereign authoritative reads carry identity attestation, purpose
  and least provision scope; every attested read requires a per-request
  permission review (entry-owner: permission-sovereign), a single-use
  read session with nonce + expiry, replay protection, and a metadata-only
  audit record.  The Chinese view is 星澄-only — this module never reads
  the Chinese mirror.

Governed viewers (sub-sovereigns, adjudication layers) read declarations
through this module instead of importing the raw codex package, and every
attested read declares its requester scope, purpose and explicit provision
id.  A bare requester string is never accepted as identity attestation on
its own: it must be bound to a single-use session minted by the
permission sovereign after per-request review, or — for a sovereign
reading its own declaration — a self-attested self-declaration session.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.codex_repository import (
    CODEX_DATABASE_PATH,
    load_governance_codex,
)


OFFICIAL_ENTRY: Final[str] = "governance-codex://official"
_REQUEST_PURPOSES: Final[frozenset[str]] = frozenset(
    {"self-declaration", "adjudication", "status"}
)
_SESSION_TTL_SECONDS: Final[float] = 30.0
_CODEX_READ_AUDIT_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "audit" / "codex_read_audit.jsonl"
)
_AUDIT_LOCK = threading.Lock()
_sessions: dict[str, tuple[str, str, str, str, float]] = {}
_session_lock = threading.Lock()


def official_entry() -> str:
    """Return the canonical entry URI for codex viewers (A74)."""
    return OFFICIAL_ENTRY


def official_authority_path() -> str:
    """The official storage path — the single local SQLite (A107/A173)."""
    return str(CODEX_DATABASE_PATH)


def _purge_expired_sessions(now: float) -> None:
    for key in [k for k, e in _sessions.items() if e[4] < now]:
        _sessions.pop(k, None)


def mint_codex_read_session(
    sovereign_id: str,
    *,
    requester: str,
    purpose: str,
    provision_id: str,
) -> str:
    """Mint a single-use codex read session (A174 nonce+expiry+single-use).

    The permission sovereign (entry-owner) calls this after completing the
    per-request permission review for an authoritative view.  A sovereign
    reading its own declaration (``requester == sovereign_id`` with
    ``purpose == "self-declaration"``) may self-mint, attesting its own
    identity for its own least-provision-scope read.

    Returns the opaque single-use nonce that ``official_sovereign`` must
    present and consume exactly once (replay-proof).
    """
    sid = str(sovereign_id or "").strip()
    actor = str(requester or "").strip()
    prov = str(provision_id or "").strip()
    purp = str(purpose or "").strip()
    if not sid or not actor or not prov or purp not in _REQUEST_PURPOSES:
        raise PermissionError("CODEX_SESSION_INVALID_REQUEST")
    if purp != "self-declaration" and actor != sid:
        if actor != "permission-sovereign":
            raise PermissionError("CODEX_SESSION_UNAUTHORIZED_MINTER")
    nonce = secrets.token_hex(16)
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions(now)
        _sessions[nonce] = (sid, actor, prov, purp, now + _SESSION_TTL_SECONDS)
    return nonce


def _consume_session(
    nonce: str, *, sovereign_id: str, requester: str, provision_id: str, purpose: str
) -> bool:
    """Consume a single-use session exactly once (A174 replay protection).

    Returns False for unknown, expired, mismatched or already-consumed
    nonces — a replayed read can never succeed.
    """
    if not isinstance(nonce, str) or not nonce:
        return False
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions(now)
        entry = _sessions.pop(nonce, None)
    if entry is None:
        return False
    sid, actor, prov, purp, expires_at = entry
    if expires_at < now:
        return False
    return (
        sid == str(sovereign_id)
        and actor == str(requester)
        and prov == str(provision_id)
        and purp == str(purpose)
    )


def _record_read_audit(
    *,
    requester: str,
    sovereign_id: str,
    provision_id: str,
    purpose: str,
    result: str,
) -> None:
    """Append a metadata-only codex-read audit entry (A174 audit).

    A174 FORBID:content-in-audit — the record carries only identity,
    scope, purpose and outcome metadata, never codex content, hashes or
    excerpts.
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "entry": OFFICIAL_ENTRY,
        "requester": str(requester),
        "sovereign_id": str(sovereign_id),
        "provision_id": str(provision_id),
        "purpose": str(purpose),
        "result": str(result),
    }
    _CODEX_READ_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, _CODEX_READ_AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _validate_read_request(
    sid: str, actor: str, prov: str, purp: str, session_nonce: str
) -> str | None:
    """Return a denial reason string, or None when the request is valid.

    Fail-closed A174 gate: identity attestation, explicit provision id
    (least scope), governed purpose, and a valid single-use session
    (replay-proof, expiry-bound) are all mandatory.
    """
    if not sid or not actor:
        return "DENIED_EMPTY_IDENTITY"
    if prov != sid:
        return "DENIED_SCOPE_MISMATCH"
    if purp not in _REQUEST_PURPOSES:
        return "DENIED_PURPOSE"
    if not _consume_session(
        session_nonce, sovereign_id=sid, requester=actor,
        provision_id=prov, purpose=purp,
    ):
        return "DENIED_SESSION_REPLAY_OR_INVALID"
    return None


def official_sovereign(
    sovereign_id: str,
    *,
    requester: str,
    purpose: str,
    provision_id: str,
    session_nonce: str,
) -> Any | None:
    """Read one sovereign declaration from the official SQLite authority.

    A174 authoritative-view controls (all mandatory, fail-closed):

    * ``requester`` — non-empty identity attestation; a bare string is
      never accepted without a bound single-use session.
    * ``provision_id`` — explicit provision id (A74); must equal the
      sovereign id being read (least provision scope).
    * ``purpose`` — governed purpose (self-declaration / adjudication /
      status).
    * ``session_nonce`` — a single-use read session minted by the
      permission sovereign after per-request review (or self-minted for a
      self-declaration); consumed exactly once (replay-proof, expiry-bound).

    A metadata-only audit record is written for every attempt (A174 audit;
    content-in-audit is forbidden).  The Chinese language mirror is never
    read here (A38/A173: it has no authority).
    """
    sid = str(sovereign_id or "").strip()
    actor = str(requester or "").strip()
    prov = str(provision_id or "").strip()
    purp = str(purpose or "").strip()
    denial = _validate_read_request(sid, actor, prov, purp, session_nonce)
    if denial is not None:
        _record_read_audit(
            requester=actor, sovereign_id=sid, provision_id=prov,
            purpose=purp, result=denial,
        )
        return None
    codex = load_governance_codex()
    for item in codex.sovereigns:
        if item.id == sid:
            _record_read_audit(
                requester=actor, sovereign_id=sid, provision_id=prov,
                purpose=purp, result="GRANTED",
            )
            return item
    _record_read_audit(
        requester=actor, sovereign_id=sid, provision_id=prov,
        purpose=purp, result="NOT_FOUND",
    )
    return None


def official_self_declaration(sovereign_id: str) -> Any | None:
    """Read a sovereign's own declaration via a self-attested session.

    Convenience for the common import-time self-declaration case: the
    sovereign self-mints a single-use session (requester == sovereign_id,
    purpose == "self-declaration", provision_id == sovereign_id) and reads
    its own declaration through the official entry.  All A174 controls
    (nonce, expiry, single-use, replay protection, audit, explicit
    provision id, non-empty requester) remain enforced.
    """
    sid = str(sovereign_id or "").strip()
    if not sid:
        return None
    nonce = mint_codex_read_session(
        sid, requester=sid, purpose="self-declaration", provision_id=sid,
    )
    return official_sovereign(
        sid, requester=sid, purpose="self-declaration",
        provision_id=sid, session_nonce=nonce,
    )


__all__ = [
    "OFFICIAL_ENTRY",
    "mint_codex_read_session",
    "official_authority_path",
    "official_entry",
    "official_self_declaration",
    "official_sovereign",
]
