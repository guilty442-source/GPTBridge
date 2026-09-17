"""Git audit record schema evolution (A375 AUDIT; codex 325/326).

The flat ``git_tier_audit.jsonl`` ledger and the hash chain
``git_audit_chain/current.jsonl`` both started without a schema version.
That legacy shape is ``AuditRecordV1``.  New records written through the
schema-aware write path carry ``schema_version = 2`` (``AuditRecordV2``)
plus the ``hook_generation`` that produced them.  Existing records are
never rewritten: the reader accepts both generations and preserves
``original_schema_version`` on every normalized record.

Write path (opt-in; the protected ``git_tiers.audit_log`` is unchanged):

* ``stamp_audit_record`` — return a copy stamped with ``schema_version`` /
  ``hook_generation`` (existing payload untouched).
* ``append_audit_record`` — append a stamped record to the flat ledger.
* ``append_chained_audit_record`` — append a stamped record to the hash
  chain (sequence / previous_hash / record_hash added by ``audit_chain``).
* ``audit_log_v2`` / ``chained_audit_log_v2`` — adapters matching the
  existing ``audit_log`` / ``chained_audit_log`` signatures.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

CURRENT_AUDIT_SCHEMA_VERSION = 2
LEGACY_AUDIT_SCHEMA_VERSION = 1
SCHEMA_VERSION_FIELD = "schema_version"
HOOK_GENERATION_FIELD = "hook_generation"
CHAIN_DIR_ENV = "GPTBRIDGE_AUDIT_CHAIN_DIR"
CHAIN_CURRENT_FILE = "current.jsonl"

_APPEND_LOCK = threading.Lock()


class AuditRecordError(ValueError):
    """A record does not match a known audit schema generation."""


def _as_int(value: Any, default: int | None = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class NormalizedAuditRecord:
    """Schema-generation independent view of one audit record.

    The field set is the frozen 25-field normalized audit contract (codex
    A498 machine-schema registry; ``AUDIT_EVENT_V1`` alignment).  Values are
    mapped from the v1/v2 payload as-is; a field the source generation does
    not carry is ``None``/empty — never invented.  ``original_schema_version``
    preserves the generation the record was read with while ``schema_version``
    is the declared/derived schema version of the payload.
    """

    sequence: int | None = None
    schema_version: int = LEGACY_AUDIT_SCHEMA_VERSION
    timestamp: str = ""
    command_id: str = ""
    transaction_id: str = ""
    proposal_id: str = ""
    capability_id: str = ""
    actor: str = ""
    worker_id: str = ""
    task_id: str = ""
    operation: str = ""
    tier: int = 0
    command_digest: str = ""
    phase: str = ""
    result: str = ""
    returncode: int | None = None
    revision_before: str = ""
    revision_after: str = ""
    policy_version: str = ""
    hook_generation: str = ""
    previous_hash: str = ""
    record_hash: str = ""
    duration_ms: int | None = None
    original_schema_version: int = LEGACY_AUDIT_SCHEMA_VERSION
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_legacy(self) -> bool:
        return self.original_schema_version < CURRENT_AUDIT_SCHEMA_VERSION

    # Legacy read-only views over the preserved payload.  These are *not*
    # part of the 25-field normalized contract; they only keep pre-contract
    # attribute access working without adding fields.
    @property
    def command(self) -> str:
        return _as_str(self.payload.get("command"))

    @property
    def approved(self) -> bool:
        return _as_bool(self.payload.get("approved"))

    @property
    def detail(self) -> str:
        return _as_str(self.payload.get("detail"))

    @property
    def epoch(self) -> int | None:
        return _as_int(self.payload.get("epoch"), None)


def _normalized_from(version: int, payload: Mapping[str, Any]) -> NormalizedAuditRecord:
    body = dict(payload)
    declared = _as_int(body.get(SCHEMA_VERSION_FIELD), None)
    schema_version = declared if declared is not None else version
    return NormalizedAuditRecord(
        sequence=_as_int(body.get("sequence"), None),
        schema_version=schema_version,
        timestamp=_as_str(body.get("timestamp")),
        command_id=_as_str(body.get("command_id")),
        transaction_id=_as_str(body.get("transaction_id")),
        proposal_id=_as_str(body.get("proposal_id")),
        capability_id=_as_str(body.get("capability_id")),
        actor=_as_str(body.get("actor")),
        worker_id=_as_str(body.get("worker_id")),
        task_id=_as_str(body.get("task_id")),
        operation=_as_str(body.get("operation")),
        tier=_as_int(body.get("tier"), 0) or 0,
        command_digest=_as_str(body.get("command_digest")),
        phase=_as_str(body.get("phase")),
        result=_as_str(body.get("result")),
        returncode=_as_int(body.get("returncode"), None),
        revision_before=_as_str(body.get("revision_before")),
        revision_after=_as_str(body.get("revision_after")),
        policy_version=_as_str(body.get("policy_version")),
        hook_generation=_as_str(body.get(HOOK_GENERATION_FIELD)),
        previous_hash=_as_str(body.get("previous_hash")),
        record_hash=_as_str(body.get("record_hash")),
        duration_ms=_as_int(body.get("duration_ms"), None),
        original_schema_version=version,
        payload=body,
    )


@dataclass(frozen=True)
class AuditRecordV1:
    """Legacy audit record: no ``schema_version`` field (or explicit 1)."""

    SCHEMA_VERSION = LEGACY_AUDIT_SCHEMA_VERSION
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AuditRecordV1:
        if not isinstance(payload, Mapping):
            raise AuditRecordError("audit record payload must be a mapping")
        declared = payload.get(SCHEMA_VERSION_FIELD)
        if declared is not None and _as_int(declared, None) != cls.SCHEMA_VERSION:
            raise AuditRecordError(
                f"record is not schema v{cls.SCHEMA_VERSION}: {declared!r}"
            )
        return cls(payload=dict(payload))

    def normalize(self) -> NormalizedAuditRecord:
        return _normalized_from(self.SCHEMA_VERSION, self.payload)


@dataclass(frozen=True)
class AuditRecordV2:
    """Current audit record: declares ``schema_version >= 2``."""

    SCHEMA_VERSION = CURRENT_AUDIT_SCHEMA_VERSION
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AuditRecordV2:
        if not isinstance(payload, Mapping):
            raise AuditRecordError("audit record payload must be a mapping")
        declared = _as_int(payload.get(SCHEMA_VERSION_FIELD), None)
        if declared is None or declared < cls.SCHEMA_VERSION:
            raise AuditRecordError(
                f"record is not schema v{cls.SCHEMA_VERSION}+: {declared!r}"
            )
        return cls(payload=dict(payload))

    @property
    def declared_schema_version(self) -> int:
        return _as_int(self.payload.get(SCHEMA_VERSION_FIELD), 0) or 0

    def normalize(self) -> NormalizedAuditRecord:
        return _normalized_from(self.declared_schema_version, self.payload)


def parse_audit_record(payload: Mapping[str, Any]) -> NormalizedAuditRecord:
    """Parse a legacy or current payload into a normalized record.

    Unknown *higher* generations are read through the v2 shape while the
    original version number is preserved, so a newer writer never makes an
    older reader drop history (v2+ are supersets by contract).
    """
    if not isinstance(payload, Mapping):
        raise AuditRecordError("audit record payload must be a mapping")
    declared = payload.get(SCHEMA_VERSION_FIELD)
    if declared is None:
        return AuditRecordV1.from_payload(payload).normalize()
    version = _as_int(declared, None)
    if version is None or version < LEGACY_AUDIT_SCHEMA_VERSION:
        raise AuditRecordError(f"unknown audit schema version: {declared!r}")
    if version == LEGACY_AUDIT_SCHEMA_VERSION:
        return AuditRecordV1.from_payload(payload).normalize()
    return AuditRecordV2.from_payload(payload).normalize()


def iter_audit_records(
    path: str | Path, *, strict: bool = False
) -> Iterator[NormalizedAuditRecord]:
    """Yield normalized records from one JSONL ledger.

    ``strict=False`` (default) skips malformed lines so a torn tail never
    makes the whole history unreadable; ``strict=True`` raises instead.
    """
    ledger = Path(path)
    with ledger.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                if strict:
                    raise AuditRecordError(
                        f"{ledger}:{line_number}: invalid JSON: {error}"
                    ) from error
                continue
            try:
                yield parse_audit_record(payload)
            except AuditRecordError:
                if strict:
                    raise
                continue


def read_audit_records(
    path: str | Path, *, strict: bool = False
) -> list[NormalizedAuditRecord]:
    return list(iter_audit_records(path, strict=strict))


def _default_ledger_path() -> Path:
    from governance_rule.execution import git_tiers

    return Path(git_tiers.AUDIT_LEDGER_PATH)


def chain_ledger_dir() -> Path:
    override = os.environ.get(CHAIN_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "audit" / "git_audit_chain"


def chain_ledger_path() -> Path:
    return chain_ledger_dir() / CHAIN_CURRENT_FILE


def audit_ledger_paths() -> tuple[Path, Path]:
    """(legacy flat ledger, chained current segment)."""
    return _default_ledger_path(), chain_ledger_path()


def iter_all_audit_records(*, strict: bool = False) -> Iterator[NormalizedAuditRecord]:
    for path in audit_ledger_paths():
        if path.is_file():
            yield from iter_audit_records(path, strict=strict)


def read_all_audit_records(*, strict: bool = False) -> list[NormalizedAuditRecord]:
    return list(iter_all_audit_records(strict=strict))


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
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with _APPEND_LOCK, path.open("a", encoding="utf-8") as handle:
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
    "AuditRecordError",
    "AuditRecordV1",
    "AuditRecordV2",
    "CHAIN_CURRENT_FILE",
    "CHAIN_DIR_ENV",
    "CURRENT_AUDIT_SCHEMA_VERSION",
    "HOOK_GENERATION_FIELD",
    "LEGACY_AUDIT_SCHEMA_VERSION",
    "NormalizedAuditRecord",
    "SCHEMA_VERSION_FIELD",
    "append_audit_record",
    "append_chained_audit_record",
    "audit_ledger_paths",
    "audit_log_v2",
    "build_audit_entry",
    "chain_ledger_dir",
    "chain_ledger_path",
    "chained_audit_log_v2",
    "iter_all_audit_records",
    "iter_audit_records",
    "parse_audit_record",
    "read_all_audit_records",
    "read_audit_records",
    "stamp_audit_record",
]
