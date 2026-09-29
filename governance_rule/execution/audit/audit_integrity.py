"""Integrity chain, tamper-state and fail-closed audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_archive_restore_test(root: Path, errors: list[str]) -> None:
    """Verify archive restore test migration (061) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "061_archive_restore_test.sql"
    if not migration.is_file():
        errors.append("Archive restore test migration 061 is missing")
        return
    text = read_text_cached(migration)
    for required in ("archive_restore_test", "record_restore_test",
                     "get_failed_restore_tests",
                     "schema_check_passed", "row_count_match",
                     "hash_verify_passed", "query_test_passed",
                     "overall_passed"):
        if required not in text:
            errors.append(f"Archive restore test migration 061 is missing: {required}")

def check_capacity_quota(root: Path, errors: list[str]) -> None:
    """Verify capacity quota migration (062) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "062_capacity_quota.sql"
    if not migration.is_file():
        errors.append("Capacity quota migration 062 is missing")
        return
    text = read_text_cached(migration)
    for required in ("capacity_quota", "update_capacity_measurement",
                     "check_capacity_status",
                     "soft_limit_mb", "hard_limit_mb",
                     "archive_threshold_mb", "emergency_threshold_mb"):
        if required not in text:
            errors.append(f"Capacity quota migration 062 is missing: {required}")

def check_purge_audit(root: Path, errors: list[str]) -> None:
    """Verify purge audit migration (063) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "063_purge_audit.sql"
    if not migration.is_file():
        errors.append("Purge audit migration 063 is missing")
        return
    text = read_text_cached(migration)
    for required in ("purge_audit_log", "record_purge", "verify_purge",
                     "get_purge_history",
                     "previous_hash", "deleted_from", "audit_hash"):
        if required not in text:
            errors.append(f"Purge audit migration 063 is missing: {required}")

def check_audit_hash_chain(root: Path, errors: list[str]) -> None:
    """Verify audit hash chain migration (064) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "064_audit_hash_chain.sql"
    if not migration.is_file():
        errors.append("Audit hash chain migration 064 is missing")
        return
    text = read_text_cached(migration)
    for required in ("event_hash", "previous_event_hash", "sequence",
                     "compute_event_hash", "populate_event_hash_chain",
                     "verify_audit_chain", "get_audit_head_hash"):
        if required not in text:
            errors.append(f"Audit hash chain migration 064 is missing: {required}")

def check_reconcile_batch_digest(root: Path, errors: list[str]) -> None:
    """Verify reconcile batch digest migration (065) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "065_reconcile_batch_digest.sql"
    if not migration.is_file():
        errors.append("Reconcile batch digest migration 065 is missing")
        return
    text = read_text_cached(migration)
    for required in ("reconcile_batch_digest", "batch_hash", "result_hash",
                     "start_reconcile_batch", "complete_reconcile_batch",
                     "verify_reconcile_batch",
                     "source_generation", "first_revision", "last_revision"):
        if required not in text:
            errors.append(f"Reconcile batch digest migration 065 is missing: {required}")

def check_resource_content_hash(root: Path, errors: list[str]) -> None:
    """Verify resource content hash migration (066) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "066_resource_content_hash.sql"
    if not migration.is_file():
        errors.append("Resource content hash migration 066 is missing")
        return
    text = read_text_cached(migration)
    for required in ("resource_content_hash", "resource_hash", "metadata_hash",
                     "locator_hash", "record_resource_hash",
                     "verify_resource_hash", "get_tampered_resources"):
        if required not in text:
            errors.append(f"Resource content hash migration 066 is missing: {required}")

def check_merkle_root(root: Path, errors: list[str]) -> None:
    """Verify Merkle root migration (069) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "069_merkle_root.sql"
    if not migration.is_file():
        errors.append("Merkle root migration 069 is missing")
        return
    text = read_text_cached(migration)
    for required in ("merkle_root", "compute_merkle_root", "record_merkle_root",
                     "verify_merkle_root", "get_merkle_root_for_domain",
                     "leaf_count", "leaf_hashes"):
        if required not in text:
            errors.append(f"Merkle root migration 069 is missing: {required}")

def check_integrity_snapshot(root: Path, errors: list[str]) -> None:
    """Verify integrity snapshot migration (070) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "070_integrity_snapshot.sql"
    if not migration.is_file():
        errors.append("Integrity snapshot migration 070 is missing")
        return
    text = read_text_cached(migration)
    for required in ("integrity_snapshot", "schema_hash", "audit_head_hash",
                     "resource_merkle_root", "migration_head",
                     "create_integrity_snapshot", "get_latest_snapshot",
                     "database_generation"):
        if required not in text:
            errors.append(f"Integrity snapshot migration 070 is missing: {required}")

def check_restore_verification(root: Path, errors: list[str]) -> None:
    """Verify restore verification migration (071) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "071_restore_verification.sql"
    if not migration.is_file():
        errors.append("Restore verification migration 071 is missing")
        return
    text = read_text_cached(migration)
    for required in ("restore_verification", "expected_schema_hash",
                     "actual_schema_hash", "schema_match", "audit_match",
                     "merkle_match", "overall_passed",
                     "record_restore_verification", "get_failed_restores"):
        if required not in text:
            errors.append(f"Restore verification migration 071 is missing: {required}")

def check_tamper_state(root: Path, errors: list[str]) -> None:
    """Verify tamper state migration (072) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "072_tamper_state.sql"
    if not migration.is_file():
        errors.append("Tamper state migration 072 is missing")
        return
    text = read_text_cached(migration)
    for required in ("tamper_state_registry", "record_tamper_state",
                     "resolve_tamper_state", "get_active_tamper_issues",
                     "verified", "unverified", "mismatch",
                     "tampered", "incomplete", "rebuild_required"):
        if required not in text:
            errors.append(f"Tamper state migration 072 is missing: {required}")

def check_fail_closed(root: Path, errors: list[str]) -> None:
    """Verify fail-closed migration (073) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "073_fail_closed.sql"
    if not migration.is_file():
        errors.append("Fail-closed migration 073 is missing")
        return
    text = read_text_cached(migration)
    for required in ("fail_closed_action", "trigger_fail_closed",
                     "release_fail_closed", "is_fail_closed_active",
                     "get_active_fail_closed",
                     "read_only", "quarantine", "recovery",
                     "codex_hash_mismatch", "audit_chain_broken"):
        if required not in text:
            errors.append(f"Fail-closed migration 073 is missing: {required}")

def check_version_lock(root: Path, errors: list[str]) -> None:
    """Verify version lock migration (074) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "074_version_lock.sql"
    if not migration.is_file():
        errors.append("Version lock migration 074 is missing")
        return
    text = read_text_cached(migration)
    for required in ("version_lock", "lock_version", "get_version_lock",
                     "get_all_version_locks", "component", "version_string"):
        if required not in text:
            errors.append(f"Version lock migration 074 is missing: {required}")
