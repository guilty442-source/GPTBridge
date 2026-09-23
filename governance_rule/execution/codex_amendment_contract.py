"""Codex amendment hash and formal-rule state contract (G69/G70).

This module is the reproducible worker-side contract used by the amendment
toolchain.  It does not change the official Codex and it does not claim that a
computed root is an authoritative seal: seal closure, external signatures and
version-axis publication remain governor authority.

Algorithms are deliberately explicit and deterministic:

* ``CONTENT_HASH_ALGORITHM`` — canonical JSON (UTF-8, NFC-preserving text,
  sorted object keys, compact separators, ``default=str``) followed by
  SHA-256.  This is the same canonical JSON convention already used by the
  mirror writer and audit certificate.
* ``SEARCH_DOCUMENT_HASH_ALGORITHM`` — SHA-256 over the exact UTF-8 document
  bytes/content, with no JSON wrapping.
* ``REVISION_ENTRY_HASH_ALGORITHM`` — SHA-256 over the UTF-8 pipe-joined
  canonical JSON values of the named revision fields, excluding the hash
  field itself.

``compute_seal_preview`` produces a *candidate preview* only.  It gives the
five-sovereign audit and governor a deterministic way to recompute content,
identity and full roots from a staged candidate without touching or sealing
the authority database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

CONTENT_HASH_ALGORITHM: Final[str] = "sha256-canonical-json-utf8-v1"
SEARCH_DOCUMENT_HASH_ALGORITHM: Final[str] = "sha256-utf8-content-v1"
REVISION_ENTRY_HASH_ALGORITHM: Final[str] = "sha256-canonical-utf8-pipe-v1"
SEAL_PREVIEW_SCHEMA: Final[str] = "gptbridge-codex-seal-preview/v1"

CONTENT_TABLES: Final[tuple[str, ...]] = (
    "preamble",
    "principles",
    "sections",
    "articles",
    "edicts",
    "sovereigns",
    "savings",
)

RULE_STATE_PROPOSED: Final[str] = "proposed"
RULE_STATE_DECLARED_PENDING_PARITY: Final[str] = (
    "declared-pending-evaluator-parity"
)
RULE_STATE_PARITY_VERIFIED: Final[str] = "evaluator-parity-verified"
RULE_STATE_ACTIVE: Final[str] = "active"
TERMINAL_RULE_STATES: Final[frozenset[str]] = frozenset(
    {"retired", "superseded", "withdrawn", "inactive"}
)
NONTERMINAL_RULE_STATES: Final[frozenset[str]] = frozenset(
    {
        RULE_STATE_PROPOSED,
        RULE_STATE_DECLARED_PENDING_PARITY,
        RULE_STATE_PARITY_VERIFIED,
        RULE_STATE_ACTIVE,
    }
)

RULE_STATE_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    RULE_STATE_PROPOSED: frozenset(
        {RULE_STATE_DECLARED_PENDING_PARITY, "withdrawn"}
    ),
    RULE_STATE_DECLARED_PENDING_PARITY: frozenset(
        {RULE_STATE_PARITY_VERIFIED, "withdrawn"}
    ),
    RULE_STATE_PARITY_VERIFIED: frozenset(
        {RULE_STATE_ACTIVE, "retired", "superseded", "withdrawn"}
    ),
    RULE_STATE_ACTIVE: frozenset({"retired", "superseded", "inactive"}),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def canonical_json(payload: Any) -> str:
    """Canonical JSON used by every codex amendment metadata hash."""
    return json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def content_hash(payload: Any) -> str:
    """Reproducible lineage/content hash for structured amendment payloads."""
    text = canonical_json(payload)
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()


def search_document_hash(content: str | bytes) -> str:
    """Hash one search/document payload exactly as stored or transmitted."""
    data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def revision_entry_hash(fields: Mapping[str, Any], *, exclude: Iterable[str] = ("entry_hash",)) -> str:
    """Compute the ``sha256-canonical-utf8-pipe-v1`` revision-chain hash."""
    excluded = {str(name) for name in exclude}
    parts = [
        canonical_json(fields[name])
        for name in sorted(fields)
        if str(name) not in excluded
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (str(row[1]), bool(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )


def _table_rows(
    connection: sqlite3.Connection, table: str
) -> tuple[dict[str, Any], ...]:
    columns = _table_columns(connection, table)
    if not columns:
        return ()
    names = [name for name, _ in columns]
    primary = [name for name, is_primary in columns if is_primary]
    order = ", ".join(primary) if primary else "rowid"
    rows = [
        {name: _json_safe(value) for name, value in zip(names, row)}
        for row in connection.execute(  # sql-ok: schema-introspected or fixed table identifiers
            f"SELECT {', '.join(names)} FROM {table} ORDER BY {order}"
        )
    ]
    return tuple(sorted(rows, key=canonical_json))


def _table_fingerprints(
    connection: sqlite3.Connection, tables: Sequence[str]
) -> dict[str, str]:
    return {
        table: content_hash(_table_rows(connection, table))
        for table in tables
    }


def compute_seal_preview(database: str | Path) -> dict[str, Any]:
    """Compute deterministic candidate roots for review, never authoritative sealing.

    ``content_root`` covers normative content tables, ``identity_root`` covers
    registered ``*_directory``/``*_registry`` identity/status/binding tables,
    and ``full_root`` covers every non-SQLite table.  The result also carries
    per-table fingerprints so an auditor can locate divergence without
    loading codex content into an audit log.
    """
    path = Path(database)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        all_tables = _table_names(connection)
        content_tables = tuple(
            table for table in CONTENT_TABLES if table in all_tables
        )
        identity_tables = tuple(
            table
            for table in all_tables
            if table.endswith("_directory") or table.endswith("_registry")
        )
        fingerprints = _table_fingerprints(connection, all_tables)
    finally:
        connection.close()
    return {
        "schema": SEAL_PREVIEW_SCHEMA,
        "algorithm": CONTENT_HASH_ALGORITHM,
        "content_root": content_hash(
            {table: fingerprints[table] for table in content_tables}
        ),
        "identity_root": content_hash(
            {table: fingerprints[table] for table in identity_tables}
        ),
        "full_root": content_hash(fingerprints),
        "table_count": len(all_tables),
        "content_tables": list(content_tables),
        "identity_tables": list(identity_tables),
        "table_fingerprints": fingerprints,
    }


def validate_rule_state(
    status: object,
    *,
    evaluator_registered: bool | None,
    parity_evidence: bool = False,
) -> tuple[str, ...]:
    """Validate one formal-rule lifecycle state fail-closed."""
    state = str(status or "").strip().lower()
    errors: list[str] = []
    if not state:
        return ("RULE_STATE_REQUIRED",)
    if state in TERMINAL_RULE_STATES:
        return ()
    if state not in NONTERMINAL_RULE_STATES:
        return (f"RULE_STATE_UNKNOWN:{state}",)
    if state in {
        RULE_STATE_PROPOSED,
        RULE_STATE_DECLARED_PENDING_PARITY,
        RULE_STATE_PARITY_VERIFIED,
        RULE_STATE_ACTIVE,
    } and evaluator_registered is not True:
        errors.append("RULE_EVALUATOR_REQUIRED")
    if state == RULE_STATE_PARITY_VERIFIED and not parity_evidence:
        errors.append("RULE_PARITY_EVIDENCE_REQUIRED")
    return tuple(errors)


def validate_rule_transition(
    current: object,
    target: object,
    *,
    evaluator_registered: bool | None,
    parity_evidence: bool = False,
) -> tuple[str, ...]:
    """Validate an explicit formal-rule lifecycle transition."""
    current_state = str(current or "").strip().lower()
    target_state = str(target or "").strip().lower()
    errors = list(
        validate_rule_state(
            current_state,
            evaluator_registered=evaluator_registered,
            parity_evidence=parity_evidence,
        )
    )
    if errors:
        return tuple(errors)
    if current_state in TERMINAL_RULE_STATES:
        return (f"RULE_STATE_TERMINAL:{current_state}",)
    allowed = RULE_STATE_TRANSITIONS.get(current_state, frozenset())
    if target_state not in allowed:
        return (f"RULE_STATE_TRANSITION_DENIED:{current_state}->{target_state}",)
    if target_state == RULE_STATE_ACTIVE and not parity_evidence:
        return ("RULE_PARITY_EVIDENCE_REQUIRED",)
    return validate_rule_state(
        target_state,
        evaluator_registered=evaluator_registered,
        parity_evidence=parity_evidence,
    )


__all__ = [
    "CONTENT_HASH_ALGORITHM",
    "CONTENT_TABLES",
    "NONTERMINAL_RULE_STATES",
    "REVISION_ENTRY_HASH_ALGORITHM",
    "RULE_STATE_ACTIVE",
    "RULE_STATE_DECLARED_PENDING_PARITY",
    "RULE_STATE_PARITY_VERIFIED",
    "RULE_STATE_PROPOSED",
    "RULE_STATE_TRANSITIONS",
    "SEARCH_DOCUMENT_HASH_ALGORITHM",
    "SEAL_PREVIEW_SCHEMA",
    "TERMINAL_RULE_STATES",
    "canonical_json",
    "compute_seal_preview",
    "content_hash",
    "revision_entry_hash",
    "search_document_hash",
    "validate_rule_state",
    "validate_rule_transition",
]
