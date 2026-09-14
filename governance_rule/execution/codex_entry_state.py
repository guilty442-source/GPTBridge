"""Shared state for the official codex entry (A435 access classes).

法典依據: A435 — the official entry keeps one governed vocabulary
(access classes, purposes, scope grammar, actor registers), one
metadata-only audit sink and one revocation generation.  Both the session
engine (``codex_session``) and the self-declaration reconciler
(``codex_reconcile``) share this state; it holds no codex content.

Audit is content-free by construction: records carry identity, purpose,
access class, a scope hash, codex version, correlation id and result only
(A435 FORBID:content-in-audit).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Final, Iterable


ACCESS_BOUNDED: Final[str] = "bounded-machine-lookup"
ACCESS_REVIEW: Final[str] = "review-session"
ACCESS_CHINESE: Final[str] = "xingcheng-chinese-review"

GOVERNED_PURPOSES: Final[frozenset[str]] = frozenset(
    {
        "self-declaration",
        "adjudication",
        "status",
        "global-review",
        "contract-gate",
        "diagnostics",
        "amendment-verification",
        "coordination",
        "audit",
    }
)

XINGCHENG_IDS: Final[frozenset[str]] = frozenset({"星澄", "xingcheng"})

# Non-sovereign governed components registered for bounded machine lookups
# (bounded-codex-proxy actor class; read_codex only — A435 non-content).
COMPONENT_ACTORS: Final[frozenset[str]] = frozenset(
    {
        "information-layer",
        "governance-registries",
        "startup-executor",
        "authority-reanchor-service",
        "codex-amendment-executor",
        "xingcheng-fault-diagnostics",
        "governance-audit",
    }
)

# Proxy components additionally holding review-session rights (rule text /
# adjudication evidence for the shared decision basis).
REVIEW_COMPONENT_ACTORS: Final[frozenset[str]] = frozenset(
    {"decision-layer", "governance-coordination", "governance-audit"}
)

VALID_SCOPE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "sovereign", "provision", "edicts", "articles", "principles",
        "registry", "directory", "codex", "chinese",
    }
)
BOUNDED_WILDCARD_KINDS: Final[frozenset[str]] = frozenset(
    {"registry", "directory"}
)

DEFAULT_SESSION_TTL: Final[float] = 120.0
DEFAULT_CONTEXT_TTL: Final[float] = 300.0
DIGEST_FLUSH_BOUND: Final[int] = 256

AUDIT_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "audit" / "codex_read_audit.jsonl"
)
_AUDIT_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Persistent entry state (A435): revocation generation, minted session
# nonces, and dual-key grants survive restarts so replay and revocation
# evidence cannot be lost by a process boundary.  The store is atomic
# (tmp+replace) and fail-closed: an unreadable or corrupt store denies all
# new entry operations (already-minted sessions deny on their next check).
# ---------------------------------------------------------------------------

ENTRY_STATE_PATH: Final[Path] = AUDIT_PATH.parent / "codex_entry_state.json"
_ENTRY_STATE_ENV: Final[str] = "GPTBRIDGE_CODEX_ENTRY_STATE"
_STATE_LOCK = threading.Lock()


def _state_path() -> Path:
    override = os.environ.get(_ENTRY_STATE_ENV, "").strip()
    return Path(override) if override else ENTRY_STATE_PATH


def _empty_state() -> dict[str, Any]:
    return {
        "revocation_generation": 0,
        "sessions": {},
        "grants": {},
        "consumed_nonces": {},
    }


def _load_state() -> dict[str, Any]:
    """Read the persisted entry state; fail closed when corrupt."""
    path = _state_path()
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PermissionError(f"CODEX_STATE_CORRUPT:{error.__class__.__name__}")
    if not isinstance(data, dict) or not isinstance(
        data.get("revocation_generation"), int
    ):
        raise PermissionError("CODEX_STATE_CORRUPT:schema")
    for key in ("sessions", "grants", "consumed_nonces"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _store_state(state: dict[str, Any]) -> None:
    """Atomically persist entry state; fail closed when unavailable."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as error:
        raise PermissionError(
            f"CODEX_STATE_UNAVAILABLE:{error.__class__.__name__}"
        )


def mutate_entry_state(mutator: Any) -> Any:
    """Load state, apply ``mutator`` and persist atomically under lock."""
    with _STATE_LOCK:
        state = _load_state()
        result = mutator(state)
        _store_state(state)
        return result


def read_entry_state() -> dict[str, Any]:
    """Consistent snapshot of the persisted entry state (fail closed)."""
    with _STATE_LOCK:
        return _load_state()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def scope_hash(scope: frozenset[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(scope), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def record_session_audit(
    *,
    event: str,
    actor: str,
    purpose: str,
    access_class: str,
    scope: frozenset[str],
    codex_version: int | None,
    correlation: str,
    result: str,
    request_count: int = 0,
) -> None:
    """Append a metadata-only audit record (A435 content-in-audit denied)."""
    entry = {
        "timestamp": utc_now(),
        "entry": "governance-codex://official",
        "event": str(event),
        "actor": str(actor),
        "purpose": str(purpose),
        "access_class": str(access_class),
        "scope_hash": scope_hash(scope),
        "codex_version": codex_version,
        "correlation": str(correlation),
        "result": str(result),
        "request_count": int(request_count),
    }
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK, AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
            )
    except OSError:
        pass


def revoke_codex_read_contexts() -> None:
    """Revoke every outstanding context/session (amendment, recertify)."""
    def _bump(state: dict[str, Any]) -> int:
        state["revocation_generation"] += 1
        return state["revocation_generation"]

    mutate_entry_state(_bump)


def current_revocation() -> int:
    return int(read_entry_state()["revocation_generation"])


def register_session_nonce(
    *,
    nonce: str,
    actor: str,
    purpose: str,
    access_class: str,
    scope: frozenset[str],
    codex_version: int,
    generation: int,
    expires_at: float,
) -> None:
    """Persist a minted session record (nonce uniqueness + lifecycle)."""
    def _register(state: dict[str, Any]) -> None:
        sessions = state["sessions"]
        if nonce in sessions or nonce in state["consumed_nonces"]:
            raise PermissionError("CODEX_NONCE_REPLAY")
        sessions[nonce] = {
            "actor": str(actor),
            "purpose": str(purpose),
            "access_class": str(access_class),
            "scope_hash": scope_hash(scope),
            "codex_version": int(codex_version),
            "generation": int(generation),
            "expires_at": float(expires_at),
            "closed": False,
        }

    mutate_entry_state(_register)


def close_session_nonce(nonce: str) -> None:
    """Mark a persisted session record closed (consumed, single-use)."""
    def _close(state: dict[str, Any]) -> None:
        record = state["sessions"].get(str(nonce))
        if record is not None:
            record["closed"] = True
        state["consumed_nonces"][str(nonce)] = utc_now()

    mutate_entry_state(_close)


def parse_scope(scope: Iterable[str]) -> frozenset[str]:
    items = frozenset(
        str(item).strip() for item in scope if str(item).strip()
    )
    for item in items:
        kind, separator, name = item.partition(":")
        if not separator or kind not in VALID_SCOPE_KINDS or not name:
            raise PermissionError(f"CODEX_SCOPE_MALFORMED:{item}")
    if not items:
        raise PermissionError("CODEX_SCOPE_REQUIRED")
    return items


__all__ = [
    "ACCESS_BOUNDED",
    "ACCESS_CHINESE",
    "ACCESS_REVIEW",
    "AUDIT_PATH",
    "BOUNDED_WILDCARD_KINDS",
    "COMPONENT_ACTORS",
    "DEFAULT_CONTEXT_TTL",
    "DEFAULT_SESSION_TTL",
    "DIGEST_FLUSH_BOUND",
    "ENTRY_STATE_PATH",
    "GOVERNED_PURPOSES",
    "REVIEW_COMPONENT_ACTORS",
    "VALID_SCOPE_KINDS",
    "XINGCHENG_IDS",
    "close_session_nonce",
    "current_revocation",
    "mutate_entry_state",
    "parse_scope",
    "read_entry_state",
    "record_session_audit",
    "register_session_nonce",
    "revoke_codex_read_contexts",
    "scope_hash",
    "utc_now",
]
