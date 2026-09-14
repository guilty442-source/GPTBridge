"""Shared state for the official codex entry (A435 access classes).

法典依據: A174/A435 — the official entry keeps one governed vocabulary
(access classes, purposes, scope grammar, actor registers), one
metadata-only audit sink and one revocation generation.  Both the session
engine (``codex_session``) and the self-declaration reconciler
(``codex_reconcile``) share this state; it holds no codex content.

Audit is content-free by construction: records carry identity, purpose,
access class, a scope hash, codex version, correlation id and result only
(A174 FORBID:content-in-audit).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Final, Iterable


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
_REVOCATION_LOCK = threading.Lock()
_revocation_generation = 0


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
    """Append a metadata-only audit record (A174 content-in-audit denied)."""
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
    global _revocation_generation
    with _REVOCATION_LOCK:
        _revocation_generation += 1


def current_revocation() -> int:
    with _REVOCATION_LOCK:
        return _revocation_generation


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
    "GOVERNED_PURPOSES",
    "REVIEW_COMPONENT_ACTORS",
    "VALID_SCOPE_KINDS",
    "XINGCHENG_IDS",
    "current_revocation",
    "parse_scope",
    "record_session_audit",
    "revoke_codex_read_contexts",
    "scope_hash",
    "utc_now",
]
