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