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

Module layout (A185 source-size split):

    audit_records_schema.py  schema generations + normalized read path
    audit_records_write.py   rotation/retention + stamped append + adapters
    audit_records.py         compatibility re-export (this module)
"""
from __future__ import annotations

from .audit_records_schema import (  # noqa: F401  (re-exported surface)
    CHAIN_CURRENT_FILE,
    CHAIN_DIR_ENV,
    CURRENT_AUDIT_SCHEMA_VERSION,
    HOOK_GENERATION_FIELD,
    LEGACY_AUDIT_SCHEMA_VERSION,
    SCHEMA_VERSION_FIELD,
    AuditRecordError,
    AuditRecordV1,
    AuditRecordV2,
    NormalizedAuditRecord,
    _as_bool,
    _as_int,
    _as_str,
    _default_ledger_path,
    _normalized_from,
    audit_ledger_paths,
    chain_ledger_dir,
    chain_ledger_path,
    iter_all_audit_records,
    iter_audit_records,
    parse_audit_record,
    read_all_audit_records,
    read_audit_records,
)
from .audit_records_write import (  # noqa: F401  (re-exported surface)
    _APPEND_LOCK,
    _append_line,
    _current_generation,
    _env_int,
    _prune_archives,
    append_audit_record,
    append_chained_audit_record,
    audit_log_v2,
    build_audit_entry,
    chained_audit_log_v2,
    flat_rotation_bytes,
    rotate_flat_ledger_if_large,
    stamp_audit_record,
)


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
