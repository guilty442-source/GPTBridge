"""Retention, archive, purge and lifecycle-state audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_rebuild_certification(root: Path, errors: list[str]) -> None:
    """Verify rebuild certification migration (028) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "028_rebuild_certification.sql"
    if not migration.is_file():
        errors.append("Rebuild certification migration 028 is missing")
        return
    text = read_text_cached(migration)
    for required in ("rebuild_certification", "record_rebuild_certification",
                     "is_engine_certified", "certified"):
        if required not in text:
            errors.append(f"Rebuild certification migration 028 is missing: {required}")

def check_watchdog_bloat_rpo_rto(root: Path, errors: list[str]) -> None:
    """Verify watchdog/bloat/RPO-RTO/capacity migration (029) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "029_watchdog_bloat_rpo_rto.sql"
    if not migration.is_file():
        errors.append("Watchdog/bloat/RPO-RTO migration 029 is missing")
        return
    text = read_text_cached(migration)
    for required in ("long_transaction_watchdog", "bloat_report",
                     "rpo_rto_class", "capacity_threshold",
                     "postgresql-central",
                     "warning_level", "critical_level", "fail_closed_level"):
        if required not in text:
            errors.append(f"Watchdog/bloat/RPO-RTO migration 029 is missing: {required}")

def check_readonly_domain_startup_cert(root: Path, errors: list[str]) -> None:
    """Verify read-only domain + startup cert migration (030) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "030_readonly_domain_startup_cert.sql"
    if not migration.is_file():
        errors.append("Read-only domain + startup cert migration 030 is missing")
        return
    text = read_text_cached(migration)
    for required in ("readonly_domain", "set_domain_readonly",
                     "is_domain_readonly", "startup_certification",
                     "record_startup_certification", "is_database_ready",
                     "schema_version_verified", "rls_verified",
                     "audit_append_only_verified", "authority_contract_verified"):
        if required not in text:
            errors.append(f"Read-only domain + startup cert migration 030 is missing: {required}")

def check_slo_metrics(root: Path, errors: list[str]) -> None:
    """Verify SLO metrics migration (031) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "031_slo_metrics.sql"
    if not migration.is_file():
        errors.append("SLO metrics migration 031 is missing")
        return
    text = read_text_cached(migration)
    for required in ("slo_metric", "slo_observation",
                     "record_slo_observation",
                     "central-query-p95", "transport-claim-latency",
                     "reconcile-backlog", "restore-success"):
        if required not in text:
            errors.append(f"SLO metrics migration 031 is missing: {required}")

def check_unified_lifecycle_state(root: Path, errors: list[str]) -> None:
    """Verify unified lifecycle state migration (050) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "050_unified_lifecycle_state.sql"
    if not migration.is_file():
        errors.append("Unified lifecycle state migration 050 is missing")
        return
    text = read_text_cached(migration)
    for required in ("lifecycle_state", "transition_lifecycle_state",
                     "get_lifecycle_state", "get_entities_by_state",
                     "ACTIVE", "STALE", "SUPERSEDED", "TOMBSTONED",
                     "ARCHIVED", "PURGED"):
        if required not in text:
            errors.append(f"Unified lifecycle state migration 050 is missing: {required}")

def check_transport_retention(root: Path, errors: list[str]) -> None:
    """Verify transport retention migration (051) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "051_transport_retention.sql"
    if not migration.is_file():
        errors.append("Transport retention migration 051 is missing")
        return
    text = read_text_cached(migration)
    for required in ("transport_retention_policy", "get_transport_archive_eligible",
                     "get_transport_purge_eligible",
                     "completed", "failed", "dead_letter",
                     "hot_retention_days", "archive_after_days"):
        if required not in text:
            errors.append(f"Transport retention migration 051 is missing: {required}")

def check_audit_retention_layering(root: Path, errors: list[str]) -> None:
    """Verify audit retention layering migration (052) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "052_audit_retention_layering.sql"
    if not migration.is_file():
        errors.append("Audit retention layering migration 052 is missing")
        return
    text = read_text_cached(migration)
    for required in ("audit_retention_layer", "get_audit_archive_eligible",
                     "get_audit_long_term_eligible",
                     "hot", "archive", "long_term"):
        if required not in text:
            errors.append(f"Audit retention layering migration 052 is missing: {required}")

def check_purge_queue(root: Path, errors: list[str]) -> None:
    """Verify purge queue migration (055) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "055_purge_queue.sql"
    if not migration.is_file():
        errors.append("Purge queue migration 055 is missing")
        return
    text = read_text_cached(migration)
    for required in ("purge_queue", "enqueue_purge", "approve_purge",
                     "get_purge_eligible", "mark_purged",
                     "retention_until", "purge_status"):
        if required not in text:
            errors.append(f"Purge queue migration 055 is missing: {required}")

def check_archive_catalog(root: Path, errors: list[str]) -> None:
    """Verify archive catalog migration (056) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "056_archive_catalog.sql"
    if not migration.is_file():
        errors.append("Archive catalog migration 056 is missing")
        return
    text = read_text_cached(migration)
    for required in ("archive_catalog", "register_archive", "verify_archive",
                     "find_archives", "storage_locator", "integrity_hash",
                     "record_count", "schema_version"):
        if required not in text:
            errors.append(f"Archive catalog migration 056 is missing: {required}")

def check_archive_versioning(root: Path, errors: list[str]) -> None:
    """Verify archive versioning migration (057) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "057_archive_versioning.sql"
    if not migration.is_file():
        errors.append("Archive versioning migration 057 is missing")
        return
    text = read_text_cached(migration)
    for required in ("archive_version_manifest", "mark_restore_tested",
                     "get_untested_archives",
                     "encoding", "archive_format_version", "checksum_algorithm"):
        if required not in text:
            errors.append(f"Archive versioning migration 057 is missing: {required}")

def check_retention_hold(root: Path, errors: list[str]) -> None:
    """Verify retention hold migration (059) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "059_retention_hold.sql"
    if not migration.is_file():
        errors.append("Retention hold migration 059 is missing")
        return
    text = read_text_cached(migration)
    for required in ("retention_hold", "place_hold", "release_hold",
                     "has_active_hold",
                     "audit_investigation", "governance_review",
                     "legal_hold", "compliance_hold"):
        if required not in text:
            errors.append(f"Retention hold migration 059 is missing: {required}")

def check_dependency_check(root: Path, errors: list[str]) -> None:
    """Verify dependency check migration (060) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "060_dependency_check.sql"
    if not migration.is_file():
        errors.append("Dependency check migration 060 is missing")
        return
    text = read_text_cached(migration)
    for required in ("dependency_check", "check_resource_dependencies",
                     "can_purge", "has_dependencies", "dependency_details"):
        if required not in text:
            errors.append(f"Dependency check migration 060 is missing: {required}")
