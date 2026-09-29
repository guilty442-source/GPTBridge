"""Data-layer contract and performance-baseline audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_data_layer_contract(root: Path, errors: list[str]) -> None:
    """Verify data layer contract migration (114)."""
    migration = root / "shared-layer" / "migrations" / "114_data_layer_contract.sql"
    if not migration.is_file():
        errors.append("Data layer contract migration 114 is missing")
        return
    text = read_text_cached(migration)
    for required in ("data_layer_contract", "register_data_layer_contract",
                     "activate_data_layer_contract",
                     "get_active_data_layer_contract",
                     "contract_version", "startup_order", "shutdown_order",
                     "degradation_policy", "recovery_policy"):
        if required not in text:
            errors.append(f"Data layer contract migration 114 is missing: {required}")

def check_dependency_classification(root: Path, errors: list[str]) -> None:
    """Verify dependency classification migration (115)."""
    migration = root / "shared-layer" / "migrations" / "115_dependency_classification.sql"
    if not migration.is_file():
        errors.append("Dependency classification migration 115 is missing")
        return
    text = read_text_cached(migration)
    for required in ("dependency_classification", "authority", "required",
                     "degradable", "optional", "classify_dependency",
                     "get_dependency_classification",
                     "postgresql", "vector"):
        if required not in text:
            errors.append(f"Dependency classification migration 115 is missing: {required}")

def check_integration_rule(root: Path, errors: list[str]) -> None:
    """Verify integration rule migration (125)."""
    migration = root / "shared-layer" / "migrations" / "125_integration_rule.sql"
    if not migration.is_file():
        errors.append("Integration rule migration 125 is missing")
        return
    text = read_text_cached(migration)
    for required in ("integration_rule", "get_integration_rules",
                     "check_integration_rule",
                     "central structured authority",
                     "canonical semantic index",
                     "durable workflow",
                     "bounded", "rebuildable"):
        if required not in text:
            errors.append(f"Integration rule migration 125 is missing: {required}")

def check_query_fingerprint(root: Path, errors: list[str]) -> None:
    """Verify query fingerprint migration (033) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "033_query_fingerprint.sql"
    if not migration.is_file():
        errors.append("Query fingerprint migration 033 is missing")
        return
    text = read_text_cached(migration)
    for required in ("query_fingerprint", "record_query_fingerprint",
                     "get_hot_queries", "p95_latency_ms"):
        if required not in text:
            errors.append(f"Query fingerprint migration 033 is missing: {required}")

def check_transport_hot_path_index(root: Path, errors: list[str]) -> None:
    """Verify transport hot path index migration (034) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "034_transport_hot_path_index.sql"
    if not migration.is_file():
        errors.append("Transport hot path index migration 034 is missing")
        return
    text = read_text_cached(migration)
    for required in ("tool_request_claim_path_idx", "tool_request_history",
                     "archive_completed_requests"):
        if required not in text:
            errors.append(f"Transport hot path index migration 034 is missing: {required}")

def check_audit_hot_history_separation(root: Path, errors: list[str]) -> None:
    """Verify audit hot/history separation migration (035) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "035_audit_hot_history_separation.sql"
    if not migration.is_file():
        errors.append("Audit hot/history separation migration 035 is missing")
        return
    text = read_text_cached(migration)
    for required in ("event_history", "archive_audit_events", "partition_threshold"):
        if required not in text:
            errors.append(f"Audit hot/history separation migration 035 is missing: {required}")

def check_wal_checkpoint_monitor(root: Path, errors: list[str]) -> None:
    """Verify WAL checkpoint monitor migration (036) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "036_wal_checkpoint_monitor.sql"
    if not migration.is_file():
        errors.append("WAL checkpoint monitor migration 036 is missing")
        return
    text = read_text_cached(migration)
    for required in ("wal_checkpoint_snapshot", "record_wal_checkpoint_snapshot",
                     "checkpoint_duration_ms", "wal_rate_mb_per_min"):
        if required not in text:
            errors.append(f"WAL checkpoint monitor migration 036 is missing: {required}")

def check_incremental_reconcile(root: Path, errors: list[str]) -> None:
    """Verify incremental reconcile migration (038) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "038_incremental_reconcile.sql"
    if not migration.is_file():
        errors.append("Incremental reconcile migration 038 is missing")
        return
    text = read_text_cached(migration)
    for required in ("reconcile_pending_queue", "enqueue_reconcile_pending",
                     "mark_reconciled", "get_pending_reconcile",
                     "purge_reconciled", "dirty"):
        if required not in text:
            errors.append(f"Incremental reconcile migration 038 is missing: {required}")

def check_performance_baseline(root: Path, errors: list[str]) -> None:
    """Verify performance baseline migration (039) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "039_performance_baseline.sql"
    if not migration.is_file():
        errors.append("Performance baseline migration 039 is missing")
        return
    text = read_text_cached(migration)
    for required in ("performance_baseline", "record_baseline",
                     "get_latest_baseline", "compare_baseline",
                     "p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
        if required not in text:
            errors.append(f"Performance baseline migration 039 is missing: {required}")
