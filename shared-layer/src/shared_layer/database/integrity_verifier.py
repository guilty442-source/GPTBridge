"""Integrity Verifier (H11).

Runtime helpers for integrity verification:
- Audit hash chain
- Reconcile batch digest
- Resource content hash
- SQLite database digest
- Qdrant integrity mapping
- Merkle root
- Integrity snapshot
- Restore verification
- Tamper state
- Fail-closed

Codex basis:
    A46/E22 — Audit: mandatory-ledger.
    A8/E21  — PostgreSQL: central-structured-official-data.
"""
from __future__ import annotations

import hashlib
from typing import Any


def compute_hash(*parts: str) -> str:
    """Compute SHA-256 hash of concatenated parts."""
    data = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def compute_merkle_root(leaf_hashes: list[str]) -> str | None:
    """Compute Merkle root from a list of leaf hashes."""
    if not leaf_hashes:
        return None
    level = list(leaf_hashes)
    while len(level) > 1:
        next_level: list[str] = []
        i = 0
        while i < len(level):
            if i + 1 < len(level):
                pair = (level[i] + level[i + 1]).encode("utf-8")
                next_level.append(hashlib.sha256(pair).hexdigest())
                i += 2
            else:
                next_level.append(level[i])
                i += 1
        level = next_level
    return level[0]


def populate_event_hash_chain(conn: Any, event_id: str) -> None:
    """Compute and set hash chain for an audit event."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_audit.populate_event_hash_chain(%s)",
            (event_id,),
        )
    conn.commit()


def verify_audit_chain(conn: Any, limit: int = 1000) -> list[dict[str, Any]]:
    """Verify the audit hash chain is intact."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, sequence, expected_hash, actual_hash, chain_intact "
            "FROM gptbridge_audit.verify_audit_chain(%s)",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "event_id": str(r[0]),
            "sequence": r[1],
            "expected_hash": r[2],
            "actual_hash": r[3],
            "chain_intact": r[4],
        }
        for r in rows
    ]


def get_audit_head_hash(conn: Any) -> str | None:
    """Get the latest event hash (chain head)."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_audit.get_audit_head_hash()")
        row = cur.fetchone()
    return row[0] if row else None


def start_reconcile_batch(
    conn: Any,
    module_id: str,
    source_generation: int,
    first_revision: int,
    last_revision: int,
    batch_hash: str,
) -> str:
    """Start a reconciliation batch."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.start_reconcile_batch(%s, %s, %s, %s, %s)",
            (module_id, source_generation, first_revision, last_revision,
             batch_hash),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def complete_reconcile_batch(
    conn: Any,
    reconcile_run_id: str,
    succeeded: bool,
    record_count: int,
    result_hash: str,
    failure_reason: str | None = None,
) -> None:
    """Complete a reconciliation batch."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.complete_reconcile_batch(%s, %s, %s, %s, %s)",
            (reconcile_run_id, succeeded, record_count, result_hash,
             failure_reason),
        )
    conn.commit()


def record_resource_hash(
    conn: Any,
    resource_id: str,
    resource_hash: str,
    revision: int,
    metadata_hash: str | None = None,
    locator_hash: str | None = None,
) -> None:
    """Record or update a resource content hash."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_resource_hash(%s, %s, %s, %s, %s)",
            (resource_id, resource_hash, revision, metadata_hash, locator_hash),
        )
    conn.commit()


def verify_resource_hash(conn: Any, resource_id: str, expected_hash: str) -> bool:
    """Verify a resource hash matches expected."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.verify_resource_hash(%s, %s)",
            (resource_id, expected_hash),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


def record_sqlite_digest(
    conn: Any,
    module_id: str,
    database_path: str,
    schema_hash: str,
    revision_head: int,
    row_count: int,
    critical_table_digest: str | None = None,
    generation: int = 0,
) -> str:
    """Record a SQLite database digest."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_sqlite_digest(%s, %s, %s, %s, %s, %s, %s)",
            (module_id, database_path, schema_hash, revision_head,
             row_count, critical_table_digest, generation),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_qdrant_integrity(
    conn: Any,
    chunk_id: str,
    resource_id: str,
    chunk_hash: str,
    embedding_version: int,
    qdrant_point_id: str,
    resource_revision: int,
) -> None:
    """Record PG chunk -> Qdrant point mapping."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_qdrant_integrity(%s, %s, %s, %s, %s, %s)",
            (chunk_id, resource_id, chunk_hash, embedding_version,
             qdrant_point_id, resource_revision),
        )
    conn.commit()


def verify_qdrant_integrity(
    conn: Any,
    chunk_id: str,
    point_exists: bool,
    hash_match: bool,
    version_match: bool,
) -> None:
    """Verify a single chunk's Qdrant mapping."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.verify_qdrant_integrity(%s, %s, %s, %s)",
            (chunk_id, point_exists, hash_match, version_match),
        )
    conn.commit()


def record_merkle_root(
    conn: Any,
    domain: str,
    domain_id: str,
    leaf_hashes: list[str],
) -> str:
    """Record a Merkle root for a domain."""
    import json
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_merkle_root(%s, %s, %s)",
            (domain, domain_id, json.dumps(leaf_hashes)),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def create_integrity_snapshot(
    conn: Any,
    database_generation: int,
    schema_hash: str,
    migration_head: int,
    audit_head_hash: str | None = None,
    resource_merkle_root: str | None = None,
    release_id: str | None = None,
    backup_id: str | None = None,
) -> str:
    """Create a new integrity snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.create_integrity_snapshot(%s, %s, %s, %s, %s, %s, %s)",
            (database_generation, schema_hash, migration_head,
             audit_head_hash, resource_merkle_root, release_id, backup_id),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_restore_verification(
    conn: Any,
    snapshot_id: str,
    restored_by: str,
    target_database: str,
    expected_schema_hash: str,
    actual_schema_hash: str,
    **expected_actual: Any,
) -> str:
    """Record a restore verification result."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_restore_verification("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (snapshot_id, restored_by, target_database,
             expected_schema_hash, actual_schema_hash,
             expected_actual.get("expected_audit_head_hash"),
             expected_actual.get("actual_audit_head_hash"),
             expected_actual.get("expected_resource_merkle_root"),
             expected_actual.get("actual_resource_merkle_root"),
             expected_actual.get("expected_generation"),
             expected_actual.get("actual_generation"),
             expected_actual.get("expected_migration_head"),
             expected_actual.get("actual_migration_head")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_tamper_state(
    conn: Any,
    entity_type: str,
    entity_id: str,
    tamper_state: str,
    detected_by: str,
    details: dict[str, Any] | None = None,
) -> str:
    """Record a tamper state for an entity."""
    import json
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_tamper_state(%s, %s, %s, %s, %s)",
            (entity_type, entity_id, tamper_state, detected_by,
             json.dumps(details) if details else None),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def trigger_fail_closed(
    conn: Any,
    trigger_type: str,
    action_taken: str,
    domain: str,
    triggered_by: str,
    trigger_entity_id: str | None = None,
    trigger_details: dict[str, Any] | None = None,
) -> str:
    """Trigger a fail-closed action."""
    import json
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.trigger_fail_closed(%s, %s, %s, %s, %s, %s)",
            (trigger_type, action_taken, domain, triggered_by,
             trigger_entity_id,
             json.dumps(trigger_details) if trigger_details else None),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def is_fail_closed_active(conn: Any, domain: str | None = None) -> bool:
    """Check if any fail-closed action is active."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_fail_closed_active(%s)",
            (domain,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


__all__ = [
    "compute_hash",
    "compute_merkle_root",
    "populate_event_hash_chain",
    "verify_audit_chain",
    "get_audit_head_hash",
    "start_reconcile_batch",
    "complete_reconcile_batch",
    "record_resource_hash",
    "verify_resource_hash",
    "record_sqlite_digest",
    "record_qdrant_integrity",
    "verify_qdrant_integrity",
    "record_merkle_root",
    "create_integrity_snapshot",
    "record_restore_verification",
    "record_tamper_state",
    "trigger_fail_closed",
    "is_fail_closed_active",
]
