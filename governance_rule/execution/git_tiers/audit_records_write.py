"""Audit record write path — v2 stamping, append, v2 adapters (A185 split).

Extracted from ``audit_records.py`` (source-size contract): flat-ledger
rotation/retention, ``stamp_audit_record``, locked append and the
``audit_log_v2`` / ``chained_audit_log_v2`` adapters matching the
protected ``git_tiers.audit_log`` / ``audit_chain.chained_audit_log``
signatures.  ``audit_records.py`` re-exports the full surface.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .audit_records_schema import (
    CURRENT_AUDIT_SCHEMA_VERSION,
    HOOK_GENERATION_FIELD,
    SCHEMA_VERSION_FIELD,
    _as_str,
    _default_ledger_path,
)

_APPEND_LOCK = threading.Lock()

# The flat ledger is append-only and unbounded by design; without a ceiling
# it grows without limit under sustained governed automation.  Rotation
# archives a full segment under ``audit/archive/flat/<YYYY-MM>/`` and pruning
# retires segments older than the retention window, so steady-state disk is
# bounded.  Both knobs are env-overridable (bytes, hours).
_FLAT_ROTATION_BYTES_ENV = "GPTBRIDGE_AUDIT_FLAT_ROTATION_BYTES"
_FLAT_ROTATION_BYTES_DEFAULT = 256 << 20
_FLAT_ARCHIVE_RETENTION_HOURS_ENV = "GPTBRIDGE_AUDIT_FLAT_RETENTION_HOURS"
_FLAT_ARCHIVE_RETENTION_HOURS_DEFAULT = 24


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value


def flat_rotation_bytes() -> int:
    return _env_int(_FLAT_ROTATION_BYTES_ENV, _FLAT_ROTATION_BYTES_DEFAULT)


def _prune_archives(archive_root: Path, retention_hours: int) -> None:
    if retention_hours <= 0 or not archive_root.is_dir():
        return
    cutoff = time.time() - retention_hours * 3600
    for candidate in archive_root.rglob("*.jsonl"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            continue


def rotate_flat_ledger_if_large(
    path: Path, *, max_bytes: int | None = None, retention_hours: int | None = None
) -> bool:
    """Archive a full flat ledger segment that exceeds ``max_bytes``.

    The rename is atomic on one volume and every flat-ledger writer reopens
    the path per append, so a segment may be rotated between appends without
    tearing a record: the next append starts a fresh file.  Returns True when
    a segment was archived (and stale archived segments pruned).
    """
    ceiling = flat_rotation_bytes() if max_bytes is None else max_bytes
    if ceiling <= 0:
        return False
    try:
        if path.stat().st_size < ceiling:
            return False
    except OSError:
        return False
    month = time.strftime("%Y-%m", time.localtime())
    archive_dir = path.parent / "archive" / "flat" / month
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{path.name}-{int(time.time())}.jsonl"
    try:
        os.replace(path, target)
    except OSError:
        return False
    _prune_archives(
        archive_dir.parent,
        _env_int(
            _FLAT_ARCHIVE_RETENTION_HOURS_ENV,
            _FLAT_ARCHIVE_RETENTION_HOURS_DEFAULT,
        )
        if retention_hours is None
        else retention_hours,
    )
    return True


def _current_generation() -> str:
    from .hook_versioning import current_hook_generation

    return current_hook_generation()


def stamp_audit_record(
    record: Mapping[str, Any],
    *,
    hook_generation: str | None = None,
    schema_version: int = CURRENT_AUDIT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Return a new record carrying ``schema_version`` + ``hook_generation``.

    The input mapping is copied; no existing record is modified or rewritten.
    """
    stamped = dict(record)
    stamped[SCHEMA_VERSION_FIELD] = int(schema_version)
    if hook_generation is None:
        hook_generation = _as_str(stamped.get(HOOK_GENERATION_FIELD)) or _current_generation()
    stamped[HOOK_GENERATION_FIELD] = hook_generation
    return stamped


def _append_line(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        rotate_flat_ledger_if_large(path)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def append_audit_record(
    record: Mapping[str, Any],
    *,
    ledger_path: str | Path | None = None,
    hook_generation: str | None = None,
) -> dict[str, Any]:
    """Append a v2-stamped record to the flat ledger; return the record."""
    stamped = stamp_audit_record(record, hook_generation=hook_generation)
    _append_line(Path(ledger_path) if ledger_path is not None else _default_ledger_path(), stamped)
    return stamped


def append_chained_audit_record(
    record: Mapping[str, Any],
    *,
    hook_generation: str | None = None,
) -> dict[str, Any]:
    """Append a v2-stamped record to the hash chain; return the chained record."""
    from .audit_chain import append_audit

    stamped = stamp_audit_record(record, hook_generation=hook_generation)
    return append_audit(stamped)


def build_audit_entry(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    *,
    operation: str = "",
    repo_snapshot: Mapping[str, Any] | None = None,
    phase: str = "decision",
    result: str = "pending",
    returncode: int | None = None,
    hook_generation: str | None = None,
) -> dict[str, Any]:
    """Build a v2 audit entry with the same fields as ``git_tiers.audit_log``."""
    from .snapshot import capture_light_snapshot

    snapshot = dict(repo_snapshot) if repo_snapshot is not None else capture_light_snapshot()
    if not operation:
        operation = command.strip().split()[0] if command.strip() else "unknown"
    entry: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "tier": tier,
        "operation": operation,
        "command": command,
        "actor": actor,
        "approved": approved,
        "phase": phase,
        "result": result,
        "returncode": returncode,
        "detail": detail,
        "head_revision": snapshot.get("head_revision", ""),
        "branch": snapshot.get("branch", "HEAD"),
        "dirty_files": snapshot.get("dirty_files", []),
        "staged_files": snapshot.get("staged_files", []),
        "untracked_files": snapshot.get("untracked_files", []),
    }
    return stamp_audit_record(entry, hook_generation=hook_generation)


def audit_log_v2(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    *,
    operation: str = "",
    repo_snapshot: Mapping[str, Any] | None = None,
    phase: str = "decision",
    result: str = "pending",
    returncode: int | None = None,
    hook_generation: str | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Schema-aware adapter for ``git_tiers.audit_log`` (flat ledger only)."""
    entry = build_audit_entry(
        tier,
        command,
        actor,
        approved,
        detail,
        operation=operation,
        repo_snapshot=repo_snapshot,
        phase=phase,
        result=result,
        returncode=returncode,
        hook_generation=hook_generation,
    )
    _append_line(Path(ledger_path) if ledger_path is not None else _default_ledger_path(), entry)
    return entry


def chained_audit_log_v2(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    *,
    operation: str = "",
    repo_snapshot: Mapping[str, Any] | None = None,
    phase: str = "decision",
    result: str = "pending",
    returncode: int | None = None,
    hook_generation: str | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Schema-aware adapter for ``audit_chain.chained_audit_log``.

    Writes the flat record and appends the v2-stamped record to the hash
    chain, returning the chained record (sequence / hashes included).
    """
    entry = build_audit_entry(
        tier,
        command,
        actor,
        approved,
        detail,
        operation=operation,
        repo_snapshot=repo_snapshot,
        phase=phase,
        result=result,
        returncode=returncode,
        hook_generation=hook_generation,
    )
    _append_line(Path(ledger_path) if ledger_path is not None else _default_ledger_path(), entry)
    from .audit_chain import append_audit

    return append_audit(entry)


__all__ = [
    "append_audit_record",
    "append_chained_audit_record",
    "audit_log_v2",
    "build_audit_entry",
    "chained_audit_log_v2",
    "flat_rotation_bytes",
    "rotate_flat_ledger_if_large",
    "stamp_audit_record",
    "_APPEND_LOCK",
    "_append_line",
    "_current_generation",
    "_env_int",
    "_prune_archives",
]
