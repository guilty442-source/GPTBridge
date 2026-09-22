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

import copy
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

_AUDIT_PATH_ENV: Final[str] = "GPTBRIDGE_CODEX_AUDIT_PATH"


def _audit_path() -> Path:
    """Codex read-audit ledger.  Module-relative by default; overridable
    so release payloads (which must stay immutable) can redirect writes
    to a state root."""
    override = os.environ.get(_AUDIT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parent / "audit" / "codex_read_audit.jsonl"
    )
_AUDIT_LOCK = threading.Lock()

# The codex read-audit ledger appends once per codex read; without a size
# ceiling it grows without bound.  Rotation archives a full segment under
# ``audit/archive/codex-read/<YYYY-MM>/`` and pruning retires archived
# segments older than the retention window.  Both knobs are env-overridable.
_AUDIT_ROTATION_BYTES_ENV: Final[str] = "GPTBRIDGE_CODEX_AUDIT_ROTATION_BYTES"
_AUDIT_ROTATION_BYTES_DEFAULT: Final[int] = 256 << 20
_AUDIT_RETENTION_HOURS_ENV: Final[str] = "GPTBRIDGE_CODEX_AUDIT_RETENTION_HOURS"
_AUDIT_RETENTION_HOURS_DEFAULT: Final[int] = 24


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _rotate_codex_read_audit_if_large() -> None:
    """Archive a full codex read-audit segment that exceeds the ceiling."""
    ceiling = _env_int(_AUDIT_ROTATION_BYTES_ENV, _AUDIT_ROTATION_BYTES_DEFAULT)
    if ceiling <= 0:
        return
    audit_path = _audit_path()
    try:
        if audit_path.stat().st_size < ceiling:
            return
    except OSError:
        return
    month = time.strftime("%Y-%m", time.localtime())
    archive_dir = audit_path.parent / "archive" / "codex-read" / month
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{audit_path.name}-{int(time.time())}.jsonl"
    try:
        os.replace(audit_path, target)
    except OSError:
        return
    retention = _env_int(_AUDIT_RETENTION_HOURS_ENV, _AUDIT_RETENTION_HOURS_DEFAULT)
    if retention > 0:
        cutoff = time.time() - retention * 3600
        for candidate in archive_dir.parent.rglob("*.jsonl"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue

# ---------------------------------------------------------------------------
# Persistent entry state (A435): revocation generation, minted session
# nonces, and dual-key grants survive restarts so replay and revocation
# evidence cannot be lost by a process boundary.  The store is atomic
# (tmp+replace) and fail-closed: an unreadable or corrupt store denies all
# new entry operations (already-minted sessions deny on their next check).
# ---------------------------------------------------------------------------

ENTRY_STATE_PATH: Final[Path] = _audit_path().parent / "codex_entry_state.json"
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


# The persisted store is read on every official-entry operation (session
# mint, per-read revocation check, grant verify).  Keying the parsed state
# on ``st_mtime_ns``/size collapses those reads to one ``stat`` while any
# writer — this process or another — still invalidates immediately because
# the atomic replace always produces a fresh mtime.  Callers receive a
# deep copy so mutation of the returned mapping can never poison the cache.
_entry_state_cache: dict[tuple[int, int], dict[str, Any]] = {}


def _load_state() -> dict[str, Any]:
    """Read the persisted entry state; fail closed when corrupt.

    Retries the brief window in which ``os.replace`` swap makes the path
    resolve as missing, and tolerates a concurrent writer's transient
    share conflict; genuinely corrupt content still denies (fail-closed).
    """
    path = _state_path()
    key: tuple[int, int] | None = None
    try:
        stat_result = path.stat()
        if stat_result.st_size:
            key = (stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _entry_state_cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    last_os_error: OSError | None = None
    for _attempt in range(5):
        try:
            if not path.is_file():
                return _empty_state()
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            time.sleep(0.01)
            continue
        except OSError as error:
            last_os_error = error
            time.sleep(0.01)
            continue
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PermissionError(
                f"CODEX_STATE_CORRUPT:{error.__class__.__name__}"
            )
    else:
        raise PermissionError(
            f"CODEX_STATE_UNAVAILABLE:{last_os_error.__class__.__name__}"
            if last_os_error is not None
            else "CODEX_STATE_UNAVAILABLE"
        )
    if not isinstance(data, dict) or not isinstance(
        data.get("revocation_generation"), int
    ):
        raise PermissionError("CODEX_STATE_CORRUPT:schema")
    for section in ("sessions", "grants", "consumed_nonces"):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    if key is not None:
        _entry_state_cache.clear()
        _entry_state_cache[key] = copy.deepcopy(data)
    return data


def _store_state(state: dict[str, Any]) -> None:
    """Atomically persist entry state; fail closed when unavailable.

    The temporary file is unique per process and thread so concurrent
    writers (e.g. the live system and a test worker) never swap a
    half-written shared temp file into place.
    """
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PermissionError(
            f"CODEX_STATE_UNAVAILABLE:{error.__class__.__name__}"
        )
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n"
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
            # Publish the freshly written state under its new mtime/size so
            # the next reader hits the cache instead of re-reading the file
            # this process just wrote.
            try:
                stat_result = path.stat()
                _entry_state_cache.clear()
                _entry_state_cache[
                    (stat_result.st_mtime_ns, stat_result.st_size)
                ] = copy.deepcopy(state)
            except OSError:
                _entry_state_cache.clear()
            return
        except OSError as error:
            last_error = error
            time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    raise PermissionError(
        f"CODEX_STATE_UNAVAILABLE:{last_error.__class__.__name__}"
        if last_error is not None
        else "CODEX_STATE_UNAVAILABLE"
    )


# Retention bounds for persisted entries.  A session record is dead once
# expired (the nonce can never be presented again) and a consumed marker is
# dead once every session that could mint it has expired; grants are dead
# at expiry.  A short grace window keeps recently-closed records available
# for forensics while bounding the store — without it the file grows one
# record per session open and every entry operation re-reads the full map.
_SESSION_RETENTION_SECONDS: Final[float] = 300.0
_CONSUMED_RETENTION_SECONDS: Final[float] = 3600.0
_GRANT_RETENTION_SECONDS: Final[float] = 300.0


def _prune_expired(state: dict[str, Any], now: float) -> None:
    """Drop dead session/grant/consumed records; live entries untouched."""
    sessions = state.get("sessions")
    if isinstance(sessions, dict):
        state["sessions"] = {
            nonce: record
            for nonce, record in sessions.items()
            if float(record.get("expires_at", 0.0)) > now - _SESSION_RETENTION_SECONDS
        }
    grants = state.get("grants")
    if isinstance(grants, dict):
        state["grants"] = {
            nonce: record
            for nonce, record in grants.items()
            if float(record.get("expires_at", 0.0)) > now - _GRANT_RETENTION_SECONDS
        }
    consumed = state.get("consumed_nonces")
    if isinstance(consumed, dict):
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - _CONSUMED_RETENTION_SECONDS)
        )
        state["consumed_nonces"] = {
            nonce: stamp
            for nonce, stamp in consumed.items()
            if str(stamp) >= cutoff
        }


def mutate_entry_state(mutator: Any) -> Any:
    """Load state, apply ``mutator`` and persist atomically under lock."""
    with _STATE_LOCK:
        state = _load_state()
        result = mutator(state)
        _prune_expired(state, time.time())
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
    audit_path = _audit_path()
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            _rotate_codex_read_audit_if_large()
        with _AUDIT_LOCK, audit_path.open("a", encoding="utf-8") as handle:
            if os.name == "nt":
                # Cross-process byte-range lock: two processes appending to
                # the same ledger must never interleave a record.
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    handle.seek(0, 2)
                    handle.write(
                        json.dumps(entry, ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
                    handle.flush()
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                handle.write(
                    json.dumps(entry, ensure_ascii=False, sort_keys=True)
                    + "\n"
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
