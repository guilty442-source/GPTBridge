"""PostgreSQL lifecycle for GPTBridge's canonical structured data.

PostgreSQL owns central structured data, shared transport, and audit roles.
The local SQLite package is limited to owner-private state and bounded,
observable degraded operation that must reconcile back to PostgreSQL.
Members remain lazy so health inspection does not require an eager connection.
"""

from __future__ import annotations

from typing import Any

POSTGRESQL_CANONICAL: bool = True

_LAZY_EXPORTS = {
    "BootstrapReport": ("bootstrap", "BootstrapReport"),
    "DatabaseBootstrap": ("bootstrap", "DatabaseBootstrap"),
    "BackupOrchestrator": ("backup", "BackupOrchestrator"),
    "BackupResult": ("backup", "BackupResult"),
    "DatabaseSettings": ("config", "DatabaseSettings"),
    "ConnectionManager": ("connection", "ConnectionManager"),
    "PostgreSQLDetection": ("detection", "PostgreSQLDetection"),
    "detect_postgresql": ("detection", "detect_postgresql"),
    "DatabaseHealth": ("health", "DatabaseHealth"),
    "DatabaseHealthCheck": ("health", "DatabaseHealthCheck"),
    "PostgreSQLPool": ("pool", "PostgreSQLPool"),
    "set_provenance": ("provenance", "set_provenance"),
    "clear_provenance": ("provenance", "clear_provenance"),
    "set_contract_version": ("provenance", "set_contract_version"),
    "set_migration_executor": ("provenance", "set_migration_executor"),
    "clear_migration_executor": ("provenance", "clear_migration_executor"),
    "capture_snapshot": ("permission_snapshot", "capture_snapshot"),
    "get_current_generation": ("generation_fence", "get_current_generation"),
    "bump_generation": ("generation_fence", "bump_generation"),
    "is_connection_stale": ("generation_fence", "is_connection_stale"),
    "get_stale_sqlite_databases": ("generation_fence", "get_stale_sqlite_databases"),
    "upsert_sqlite_generation": ("generation_fence", "upsert_sqlite_generation"),
    "tombstone": ("deletion_coordinator", "tombstone"),
    "advance_stage": ("deletion_coordinator", "advance_stage"),
    "get_purge_eligible": ("deletion_coordinator", "get_purge_eligible"),
    "scan_orphans": ("orphan_scanner", "scan_orphans"),
    "certify_rebuild": ("rebuild_certifier", "certify"),
    "is_rebuild_certified": ("rebuild_certifier", "is_certified"),
    "check_long_transactions": ("watchdog", "check_long_transactions"),
    "collect_bloat_report": ("watchdog", "collect_bloat_report"),
    "get_rpo_rto_classes": ("watchdog", "get_rpo_rto_classes"),
    "get_capacity_thresholds": ("watchdog", "get_capacity_thresholds"),
    "certify_startup": ("startup_certifier", "certify_startup"),
    "is_database_ready": ("startup_certifier", "is_ready"),
    "set_domain_readonly": ("readonly_domain", "set_readonly"),
    "is_domain_readonly": ("readonly_domain", "is_readonly"),
    "record_query_fingerprint": ("query_fingerprint", "record"),
    "get_hot_queries": ("query_fingerprint", "get_hot"),
    "apply_sqlite_pragma": ("sqlite_pragma_policy", "apply_pragma"),
    "get_sqlite_pragma_policy": ("sqlite_pragma_policy", "get_pragma_policy"),
    "register_sqlite_class": ("sqlite_classification", "register"),
    "get_sqlite_class": ("sqlite_classification", "get_class"),
    "list_sqlite_by_class": ("sqlite_classification", "list_by_class"),
    "check_and_checkpoint": ("sqlite_wal_governor", "check_and_checkpoint"),
    "get_wal_stats": ("sqlite_wal_governor", "get_wal_stats"),
    "BatchWriter": ("batch_writer", "BatchWriter"),
    "LocatorCache": ("locator_cache", "LocatorCache"),
    "get_default_locator_cache": ("locator_cache", "get_default_cache"),
    "record_baseline": ("performance_baseline", "record"),
    "get_latest_baseline": ("performance_baseline", "get_latest"),
    "compare_baseline": ("performance_baseline", "compare"),
    "load_release_manifest": ("release_manifest", "load_manifest"),
    "validate_runtime_compatibility": ("release_manifest", "validate_runtime"),
    "get_release_compatibility_matrix": ("release_manifest", "get_compatibility_matrix"),
    "transition_lifecycle_state": ("lifecycle_manager", "transition_state"),
    "get_lifecycle_state": ("lifecycle_manager", "get_state"),
    "enqueue_purge": ("lifecycle_manager", "enqueue_purge"),
    "get_purge_eligible": ("lifecycle_manager", "get_purge_eligible"),
    "check_resource_dependencies": ("lifecycle_manager", "check_dependencies"),
    "record_purge_audit": ("lifecycle_manager", "record_purge"),
    "place_retention_hold": ("lifecycle_manager", "place_hold"),
    "release_retention_hold": ("lifecycle_manager", "release_hold"),
    "has_active_retention_hold": ("lifecycle_manager", "has_active_hold"),
    "register_archive_catalog": ("lifecycle_manager", "register_archive"),
    # Phase H: integrity verification
    "populate_event_hash_chain": ("integrity_verifier", "populate_event_hash_chain"),
    "verify_audit_chain": ("integrity_verifier", "verify_audit_chain"),
    "get_audit_head_hash": ("integrity_verifier", "get_audit_head_hash"),
    "start_reconcile_batch": ("integrity_verifier", "start_reconcile_batch"),
    "complete_reconcile_batch": ("integrity_verifier", "complete_reconcile_batch"),
    "record_resource_content_hash": ("integrity_verifier", "record_resource_hash"),
    "verify_resource_content_hash": ("integrity_verifier", "verify_resource_hash"),
    "record_sqlite_database_digest": ("integrity_verifier", "record_sqlite_digest"),
    "record_qdrant_integrity_mapping": ("integrity_verifier", "record_qdrant_integrity"),
    "verify_qdrant_integrity_mapping": ("integrity_verifier", "verify_qdrant_integrity"),
    "record_merkle_root": ("integrity_verifier", "record_merkle_root"),
    "create_integrity_snapshot": ("integrity_verifier", "create_integrity_snapshot"),
    "record_restore_verification": ("integrity_verifier", "record_restore_verification"),
    "record_tamper_state": ("integrity_verifier", "record_tamper_state"),
    "trigger_fail_closed": ("integrity_verifier", "trigger_fail_closed"),
    "is_fail_closed_active": ("integrity_verifier", "is_fail_closed_active"),
}

__all__ = ["POSTGRESQL_CANONICAL", *_LAZY_EXPORTS]


def __getattr__(name: str) -> Any:
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module 'shared_layer.database' has no attribute {name!r}")
    module_name, member = entry
    module = __import__(
        f"shared_layer.database.{module_name}", fromlist=[member]
    )
    return getattr(module, member)