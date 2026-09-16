"""Codex, git tier, metadata contract, and embedded browser artifact checks."""

from __future__ import annotations

import json
from pathlib import Path

import governance_rule.execution.git_tiers
from governance_rule.execution.chinese_codex_mirror import load_chinese_codex_parts
from governance_rule.execution.codex_repository import (
    format_codex_version,
    load_governance_codex,
)


def check_codex_consistency(root: Path, errors: list[str]) -> None:
    """Verify the Chinese codex reference is synchronized with the authoritative codex."""
    # A279 certified tooling: governed repository load, read-only.
    codex = load_governance_codex()
    try:
        chinese = load_chinese_codex_parts(root / "governance_rule" / "codex")
        tables = chinese["tables"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        errors.append(f"Chinese codex reference is invalid: {error}")
        chinese, tables = {}, {}
    if str(chinese.get("codex_version")) != format_codex_version(
        codex.codex_version
    ):
        errors.append("Chinese codex version is not synchronized")
    expected_ids = {
        "principles": {item.id for item in codex.principles},
        "articles": {item.id for item in codex.articles},
        "edicts": {item.id for item in codex.edicts},
        "sovereigns": {item.id for item in codex.sovereigns},
    }
    for table_name, expected in expected_ids.items():
        key = "sovereign_id" if table_name == "sovereigns" else "provision_id"
        actual = {str(row.get(key)) for row in tables.get(table_name, [])}
        if actual != expected:
            errors.append(f"Chinese codex identities are not synchronized: {table_name}")
    for required_table in (
        "metadata", "revision_history", "seal_manifest", "certification_policy",
        "version_evolution_rules", "provision_identities", "provision_lineage",
    ):
        if required_table not in tables:
            errors.append(f"Chinese codex metadata is missing: {required_table}")


def check_git_tiers(root: Path, errors: list[str]) -> None:
    """Verify git tier enforcement layers and hooks exist and are correct."""
    git_tiers_source = root / "governance_rule" / "execution" / "git_tiers" / "__init__.py"
    if not git_tiers_source.is_file():
        errors.append("git tier enforcement module is missing")
    else:
        git_tiers_text = git_tiers_source.read_text(encoding="utf-8")
        if "TIER1_OPS" not in git_tiers_text or "TIER2_OPS" not in git_tiers_text or "TIER3_OPS" not in git_tiers_text:
            errors.append("git tier module is missing tier operation sets")
        if "def classify" not in git_tiers_text or "def enforce" not in git_tiers_text:
            errors.append("git tier module is missing classify/enforce functions")
        if "def audit_log" not in git_tiers_text:
            errors.append("git tier module is missing audit_log function")
        if governance_rule.execution.git_tiers.classify("unknown-governance-operation") != 3:
            errors.append("unknown git operations must fail closed as tier 3")

    git_gate_source = root / "scripts" / "git-gate.py"
    if not git_gate_source.is_file():
        errors.append("git gate wrapper is missing")
    else:
        git_gate_text = git_gate_source.read_text(encoding="utf-8")
        if "from governance_rule.execution.git_tiers import" not in git_gate_text:
            errors.append("git gate wrapper does not import git_tiers module")

    hook_root = root / "governance_rule" / "git-hooks"
    for hook_name in ("pre-commit", "pre-merge-commit", "pre-push"):
        hook_source = hook_root / hook_name
        if not hook_source.is_file():
            errors.append(f"governed Git hook is missing: {hook_name}")
    pre_push_source = hook_root / "pre-push"
    if pre_push_source.is_file():
        hook_text = pre_push_source.read_text(encoding="utf-8")
        if "GOVERNANCE_AUTHORITY_APPROVAL" not in hook_text:
            errors.append("pre-push hook does not enforce governance authority approval")
        if "merge-base" not in hook_text or "refs/tags/" not in hook_text:
            errors.append("pre-push hook does not detect non-fast-forward or tag rewrites")


def check_metadata_contract(root: Path, errors: list[str]) -> None:
    """Verify metadata contract module and data ownership document exist."""
    metadata_contract = root / "shared-layer" / "src" / "shared_layer" / "metadata_contract.py"
    if not metadata_contract.is_file():
        errors.append("metadata contract module is missing")
    else:
        contract_text = metadata_contract.read_text(encoding="utf-8")
        for required in ("FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
                         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
                         "FIELD_STATUS", "ResourceMetadata", "validate_qdrant_payload"):
            if required not in contract_text:
                errors.append(f"metadata contract is missing: {required}")

    ownership_doc = root / "shared-layer" / "docs" / "DATA_OWNERSHIP_CONTRACT.md"
    if not ownership_doc.is_file():
        errors.append("data ownership contract document is missing")


def check_reconcile_modules(root: Path, errors: list[str]) -> None:
    """Verify reconcile state store and system reconciliation owner exist."""
    reconcile_module = root / "shared-layer" / "src" / "shared_layer" / "reconcile.py"
    if not reconcile_module.is_file():
        errors.append("reconcile state store module is missing")
    else:
        reconcile_text = reconcile_module.read_text(encoding="utf-8")
        if "ReconcileStateStore" not in reconcile_text:
            errors.append("reconcile module is missing ReconcileStateStore class")
        for forbidden_decision in (
            "class ReconcileService",
            "def _push_to_central",
            "def _pull_from_central",
        ):
            if forbidden_decision in reconcile_text:
                errors.append(
                    f"shared layer contains reconciliation decision: {forbidden_decision}"
                )
    reconciliation_owner = (
        root / "main-system" / "src-core" / "core_system" / "data_reconciliation.py"
    )
    if not reconciliation_owner.is_file():
        errors.append("system data reconciliation owner is missing")
    elif "class ReconcileService" not in reconciliation_owner.read_text(encoding="utf-8"):
        errors.append("system data reconciliation owner lacks decision service")


def check_sql_migrations(root: Path, errors: list[str]) -> None:
    """Verify required SQL migration files exist."""
    migrations_dir = root / "shared-layer" / "migrations"
    for migration_name in (
        "004_global_and_module_version_tables.sql",
        "005_central_index_composite_indexes.sql",
        "006_audit_append_only_enforcement.sql",
        "007_transport_idempotency_key.sql",
        "008_rls_role_isolation.sql",
        "018_data_lineage.sql",
        "019_authority_marker.sql",
        "020_write_provenance.sql",
        "021_permission_snapshot.sql",
        "022_schema_ownership_lock.sql",
        "023_ddl_audit.sql",
        "024_contract_version_handshake.sql",
        "025_sqlite_generation_fence.sql",
        "026_workload_class.sql",
        "027_two_stage_deletion.sql",
        "028_rebuild_certification.sql",
        "029_watchdog_bloat_rpo_rto.sql",
        "030_readonly_domain_startup_cert.sql",
        "031_slo_metrics.sql",
        "032_workload_pool_query_class.sql",
        "033_query_fingerprint.sql",
        "034_transport_hot_path_index.sql",
        "035_audit_hot_history_separation.sql",
        "036_wal_checkpoint_monitor.sql",
        "037_sqlite_classification.sql",
        "038_incremental_reconcile.sql",
        "039_performance_baseline.sql",
        "040_database_release_manifest.sql",
        "041_compatibility_matrix.sql",
        "042_migration_breaking_change.sql",
        "043_query_contract_version.sql",
        "044_rls_role_migration.sql",
        "045_sqlite_template_release.sql",
        "046_qdrant_contract_version.sql",
        "047_canary_upgrade.sql",
        "048_release_audit.sql",
        "049_roll_forward.sql",
        "058_transport_priority_queue.sql",
        "050_unified_lifecycle_state.sql",
        "051_transport_retention.sql",
        "052_audit_retention_layering.sql",
        "053_sqlite_per_class_retention.sql",
        "054_qdrant_vector_lifecycle.sql",
        "055_purge_queue.sql",
        "056_archive_catalog.sql",
        "057_archive_versioning.sql",
        "059_retention_hold.sql",
        "060_dependency_check.sql",
        "061_archive_restore_test.sql",
        "062_capacity_quota.sql",
        "063_purge_audit.sql",
        "064_audit_hash_chain.sql",
        "065_reconcile_batch_digest.sql",
        "066_resource_content_hash.sql",
        "067_sqlite_database_digest.sql",
        "068_qdrant_integrity_mapping.sql",
        "069_merkle_root.sql",
        "070_integrity_snapshot.sql",
        "071_restore_verification.sql",
        "072_tamper_state.sql",
        "073_fail_closed.sql",
    ):
        if not (migrations_dir / migration_name).is_file():
            errors.append(f"SQL migration is missing: {migration_name}")


def check_sqlite_template(root: Path, errors: list[str]) -> None:
    """Verify the SQLite module template contains required tables."""
    sqlite_template = root / "shared-layer" / "sql" / "sqlite_module_template.sql"
    if not sqlite_template.is_file():
        errors.append("SQLite module template is missing")
    else:
        template_text = sqlite_template.read_text(encoding="utf-8")
        for required_table in ("schema_version", "module_metadata",
                               "resource_metadata", "audit_event", "reconcile_state"):
            if required_table not in template_text:
                errors.append(f"SQLite module template is missing table: {required_table}")
        # Authority marker (migration 019) + write provenance (migration 020)
        for required_column in ("authority_class", "executor_id", "correlation_id"):
            if required_column not in template_text:
                errors.append(
                    f"SQLite module template is missing column: {required_column}"
                )


def check_data_lineage(root: Path, errors: list[str]) -> None:
    """Verify data lineage migration (018) defines required objects."""
    lineage_migration = root / "shared-layer" / "migrations" / "018_data_lineage.sql"
    if not lineage_migration.is_file():
        errors.append("Data lineage migration 018 is missing")
        return
    text = lineage_migration.read_text(encoding="utf-8")
    for required_object in (
        "gptbridge_index.data_lineage",
        "auto_populate_lineage",
        "resource_lineage_populate",
        "resource_lineage",
    ):
        if required_object not in text:
            errors.append(
                f"Data lineage migration 018 is missing object: {required_object}"
            )


def check_authority_marker(root: Path, errors: list[str]) -> None:
    """Verify authority marker migration (019) defines required columns."""
    authority_migration = root / "shared-layer" / "migrations" / "019_authority_marker.sql"
    if not authority_migration.is_file():
        errors.append("Authority marker migration 019 is missing")
        return
    text = authority_migration.read_text(encoding="utf-8")
    for required_token in (
        "authority_class",
        "central-official",
        "module-private",
        "degraded-copy",
    ):
        if required_token not in text:
            errors.append(
                f"Authority marker migration 019 is missing token: {required_token}"
            )


def check_write_provenance(root: Path, errors: list[str]) -> None:
    """Verify write provenance migration (020) defines required objects."""
    provenance_migration = root / "shared-layer" / "migrations" / "020_write_provenance.sql"
    if not provenance_migration.is_file():
        errors.append("Write provenance migration 020 is missing")
        return
    text = provenance_migration.read_text(encoding="utf-8")
    for required_object in (
        "executor_id",
        "correlation_id",
        "source_revision",
        "auto_populate_provenance",
        "resource_provenance_populate",
        "audit_event_provenance_populate",
    ):
        if required_object not in text:
            errors.append(
                f"Write provenance migration 020 is missing object: {required_object}"
            )


def check_provenance_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime provenance helper module exists and exports."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "provenance.py"
    if not helper.is_file():
        errors.append("Provenance helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    for required_symbol in ("set_provenance", "clear_provenance", "gptbridge.actor_id"):
        if required_symbol not in text:
            errors.append(
                f"Provenance helper is missing symbol: {required_symbol}"
            )


def check_permission_snapshot(root: Path, errors: list[str]) -> None:
    """Verify permission snapshot migration (021) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "021_permission_snapshot.sql"
    if not migration.is_file():
        errors.append("Permission snapshot migration 021 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "permission_snapshot",
        "capture_permission_snapshot",
        "evaluated_roles",
        "evaluated_policies",
        "decision_summary",
    ):
        if required not in text:
            errors.append(f"Permission snapshot migration 021 is missing: {required}")


def check_schema_ownership_lock(root: Path, errors: list[str]) -> None:
    """Verify schema ownership lock migration (022) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "022_schema_ownership_lock.sql"
    if not migration.is_file():
        errors.append("Schema ownership lock migration 022 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "gptbridge_migration_owner",
        "ddl_guard",
        "ddl_guard_trigger",
        "is_migration_executor",
    ):
        if required not in text:
            errors.append(f"Schema ownership lock migration 022 is missing: {required}")


def check_ddl_audit(root: Path, errors: list[str]) -> None:
    """Verify DDL audit migration (023) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "023_ddl_audit.sql"
    if not migration.is_file():
        errors.append("DDL audit migration 023 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "ddl_event",
        "audit_ddl_event",
        "ddl_audit_trigger",
        "command_tag",
    ):
        if required not in text:
            errors.append(f"DDL audit migration 023 is missing: {required}")


def check_contract_handshake(root: Path, errors: list[str]) -> None:
    """Verify contract version handshake migration (024) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "024_contract_version_handshake.sql"
    if not migration.is_file():
        errors.append("Contract version handshake migration 024 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "contract_version",
        "check_contract_compatibility",
        "enforce_contract_version",
        "contract_version_fence",
        "min_compatible_version",
    ):
        if required not in text:
            errors.append(f"Contract handshake migration 024 is missing: {required}")


def check_permission_snapshot_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime permission snapshot helper exists."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "permission_snapshot.py"
    if not helper.is_file():
        errors.append("Permission snapshot helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    if "capture_snapshot" not in text:
        errors.append("Permission snapshot helper is missing: capture_snapshot")


def check_sqlite_generation_fence(root: Path, errors: list[str]) -> None:
    """Verify SQLite generation fence migration (025) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "025_sqlite_generation_fence.sql"
    if not migration.is_file():
        errors.append("SQLite generation fence migration 025 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_generation", "upsert_sqlite_generation", "stale"):
        if required not in text:
            errors.append(f"SQLite generation fence migration 025 is missing: {required}")


def check_workload_class(root: Path, errors: list[str]) -> None:
    """Verify workload class migration (026) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "026_workload_class.sql"
    if not migration.is_file():
        errors.append("Workload class migration 026 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("workload_class", "interactive", "transport", "audit",
                     "reconciliation", "maintenance", "migration",
                     "statement_timeout_ms", "apply_workload_class"):
        if required not in text:
            errors.append(f"Workload class migration 026 is missing: {required}")


def check_two_stage_deletion(root: Path, errors: list[str]) -> None:
    """Verify two-stage deletion migration (027) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "027_two_stage_deletion.sql"
    if not migration.is_file():
        errors.append("Two-stage deletion migration 027 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("deletion_stage", "tombstone", "retention", "purged",
                     "tombstone_resource", "advance_deletion_stage",
                     "get_purge_eligible", "purge_after"):
        if required not in text:
            errors.append(f"Two-stage deletion migration 027 is missing: {required}")


def check_orphan_scanner(root: Path, errors: list[str]) -> None:
    """Verify the runtime orphan scanner module exists."""
    scanner = root / "shared-layer" / "src" / "shared_layer" / "database" / "orphan_scanner.py"
    if not scanner.is_file():
        errors.append("Orphan scanner module is missing")
        return
    text = scanner.read_text(encoding="utf-8")
    if "scan_orphans" not in text:
        errors.append("Orphan scanner is missing: scan_orphans")


def check_deletion_coordinator(root: Path, errors: list[str]) -> None:
    """Verify the runtime deletion coordinator module exists."""
    coordinator = root / "shared-layer" / "src" / "shared_layer" / "database" / "deletion_coordinator.py"
    if not coordinator.is_file():
        errors.append("Deletion coordinator module is missing")
        return
    text = coordinator.read_text(encoding="utf-8")
    for required in ("tombstone", "advance_stage", "get_purge_eligible"):
        if required not in text:
            errors.append(f"Deletion coordinator is missing: {required}")


def check_generation_fence_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime generation fence helper module exists."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "generation_fence.py"
    if not helper.is_file():
        errors.append("Generation fence helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    for required in ("get_current_generation", "bump_generation", "is_connection_stale"):
        if required not in text:
            errors.append(f"Generation fence helper is missing: {required}")


def check_rebuild_certification(root: Path, errors: list[str]) -> None:
    """Verify rebuild certification migration (028) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "028_rebuild_certification.sql"
    if not migration.is_file():
        errors.append("Rebuild certification migration 028 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("long_transaction_watchdog", "bloat_report",
                     "rpo_rto_class", "capacity_threshold",
                     "postgresql-central", "governance-codex-sqlite",
                     "module-sqlite", "qdrant",
                     "warning_level", "critical_level", "fail_closed_level"):
        if required not in text:
            errors.append(f"Watchdog/bloat/RPO-RTO migration 029 is missing: {required}")


def check_readonly_domain_startup_cert(root: Path, errors: list[str]) -> None:
    """Verify read-only domain + startup cert migration (030) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "030_readonly_domain_startup_cert.sql"
    if not migration.is_file():
        errors.append("Read-only domain + startup cert migration 030 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("slo_metric", "slo_observation",
                     "record_slo_observation",
                     "central-query-p95", "transport-claim-latency",
                     "reconcile-backlog", "sqlite-lock-rate",
                     "qdrant-stale-rate", "restore-success"):
        if required not in text:
            errors.append(f"SLO metrics migration 031 is missing: {required}")


def check_unified_lifecycle_state(root: Path, errors: list[str]) -> None:
    """Verify unified lifecycle state migration (050) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "050_unified_lifecycle_state.sql"
    if not migration.is_file():
        errors.append("Unified lifecycle state migration 050 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("audit_retention_layer", "get_audit_archive_eligible",
                     "get_audit_long_term_eligible",
                     "hot", "archive", "long_term"):
        if required not in text:
            errors.append(f"Audit retention layering migration 052 is missing: {required}")


def check_sqlite_per_class_retention(root: Path, errors: list[str]) -> None:
    """Verify SQLite per-class retention migration (053) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "053_sqlite_per_class_retention.sql"
    if not migration.is_file():
        errors.append("SQLite per-class retention migration 053 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_retention_policy", "get_sqlite_retention_for_class",
                     "retention_days", "archive_eligible", "purge_eligible"):
        if required not in text:
            errors.append(f"SQLite per-class retention migration 053 is missing: {required}")


def check_qdrant_vector_lifecycle(root: Path, errors: list[str]) -> None:
    """Verify Qdrant vector lifecycle migration (054) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "054_qdrant_vector_lifecycle.sql"
    if not migration.is_file():
        errors.append("Qdrant vector lifecycle migration 054 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_vector_lifecycle", "mark_vector_for_resource_state",
                     "confirm_vector_deleted", "get_vectors_pending_deletion",
                     "ACTIVE", "STALE", "RETRIEVAL_FORBIDDEN",
                     "DELETE_PENDING", "VERIFIED_DELETED"):
        if required not in text:
            errors.append(f"Qdrant vector lifecycle migration 054 is missing: {required}")


def check_purge_queue(root: Path, errors: list[str]) -> None:
    """Verify purge queue migration (055) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "055_purge_queue.sql"
    if not migration.is_file():
        errors.append("Purge queue migration 055 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("dependency_check", "check_resource_dependencies",
                     "can_purge", "has_dependencies", "dependency_details"):
        if required not in text:
            errors.append(f"Dependency check migration 060 is missing: {required}")


def check_archive_restore_test(root: Path, errors: list[str]) -> None:
    """Verify archive restore test migration (061) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "061_archive_restore_test.sql"
    if not migration.is_file():
        errors.append("Archive restore test migration 061 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("purge_audit_log", "record_purge", "verify_purge",
                     "get_purge_history",
                     "previous_hash", "deleted_from", "audit_hash"):
        if required not in text:
            errors.append(f"Purge audit migration 063 is missing: {required}")


def check_lifecycle_manager_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime lifecycle manager module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "lifecycle_manager.py"
    if not module.is_file():
        errors.append("Lifecycle manager module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("transition_state", "get_state", "enqueue_purge",
                     "get_purge_eligible", "check_dependencies",
                     "record_purge", "place_hold", "release_hold",
                     "has_active_hold", "register_archive"):
        if required not in text:
            errors.append(f"Lifecycle manager module is missing: {required}")


def check_audit_hash_chain(root: Path, errors: list[str]) -> None:
    """Verify audit hash chain migration (064) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "064_audit_hash_chain.sql"
    if not migration.is_file():
        errors.append("Audit hash chain migration 064 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("resource_content_hash", "resource_hash", "metadata_hash",
                     "locator_hash", "record_resource_hash",
                     "verify_resource_hash", "get_tampered_resources"):
        if required not in text:
            errors.append(f"Resource content hash migration 066 is missing: {required}")


def check_sqlite_database_digest(root: Path, errors: list[str]) -> None:
    """Verify SQLite database digest migration (067) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "067_sqlite_database_digest.sql"
    if not migration.is_file():
        errors.append("SQLite database digest migration 067 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_database_digest", "schema_hash", "revision_head",
                     "row_count", "critical_table_digest",
                     "record_sqlite_digest", "verify_sqlite_digest",
                     "get_tampered_sqlite_dbs"):
        if required not in text:
            errors.append(f"SQLite database digest migration 067 is missing: {required}")


def check_qdrant_integrity_mapping(root: Path, errors: list[str]) -> None:
    """Verify Qdrant integrity mapping migration (068) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "068_qdrant_integrity_mapping.sql"
    if not migration.is_file():
        errors.append("Qdrant integrity mapping migration 068 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_integrity_map", "chunk_hash", "embedding_version",
                     "qdrant_point_id", "resource_revision",
                     "record_qdrant_integrity", "verify_qdrant_integrity",
                     "get_qdrant_integrity_issues"):
        if required not in text:
            errors.append(f"Qdrant integrity mapping migration 068 is missing: {required}")


def check_merkle_root(root: Path, errors: list[str]) -> None:
    """Verify Merkle root migration (069) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "069_merkle_root.sql"
    if not migration.is_file():
        errors.append("Merkle root migration 069 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("fail_closed_action", "trigger_fail_closed",
                     "release_fail_closed", "is_fail_closed_active",
                     "get_active_fail_closed",
                     "read_only", "quarantine", "recovery",
                     "codex_hash_mismatch", "audit_chain_broken"):
        if required not in text:
            errors.append(f"Fail-closed migration 073 is missing: {required}")


def check_integrity_verifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime integrity verifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "integrity_verifier.py"
    if not module.is_file():
        errors.append("Integrity verifier module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("compute_hash", "compute_merkle_root",
                     "populate_event_hash_chain", "verify_audit_chain",
                     "get_audit_head_hash", "start_reconcile_batch",
                     "complete_reconcile_batch", "record_resource_hash",
                     "verify_resource_hash", "record_sqlite_digest",
                     "record_qdrant_integrity", "verify_qdrant_integrity",
                     "record_merkle_root", "create_integrity_snapshot",
                     "record_restore_verification", "record_tamper_state",
                     "trigger_fail_closed", "is_fail_closed_active"):
        if required not in text:
            errors.append(f"Integrity verifier module is missing: {required}")


def check_database_release_manifest(root: Path, errors: list[str]) -> None:
    """Verify database release manifest migration (040) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "040_database_release_manifest.sql"
    if not migration.is_file():
        errors.append("Database release manifest migration 040 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("database_release", "transition_release_state",
                     "get_active_release", "supersede_active_release",
                     "DRAFT", "VALIDATED", "CERTIFIED", "STAGED",
                     "ACTIVE", "SUPERSEDED", "ARCHIVED", "REJECTED"):
        if required not in text:
            errors.append(f"Database release manifest migration 040 is missing: {required}")


def check_release_manifest_file(root: Path, errors: list[str]) -> None:
    """Verify the database-release.json manifest file exists."""
    manifest = root / "shared-layer" / "database-release.json"
    if not manifest.is_file():
        errors.append("database-release.json manifest is missing")
        return
    import json
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"database-release.json is invalid: {exc}")
        return
    for key in ("release_id", "schema_version", "migration_head",
                "rls_version", "role_version", "sqlite_template_version",
                "reconcile_contract_version", "qdrant_contract_version",
                "query_contract_version", "minimum_runtime_version",
                "compatibility_range", "state"):
        if key not in data:
            errors.append(f"database-release.json is missing key: {key}")


def check_release_manifest_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime release manifest module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "release_manifest.py"
    if not module.is_file():
        errors.append("Release manifest module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("load_manifest", "validate_runtime", "get_compatibility_matrix"):
        if required not in text:
            errors.append(f"Release manifest module is missing: {required}")


def check_compatibility_matrix(root: Path, errors: list[str]) -> None:
    """Verify compatibility matrix migration (041) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "041_compatibility_matrix.sql"
    if not migration.is_file():
        errors.append("Compatibility matrix migration 041 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("release_compatibility", "check_compatibility",
                     "upsert_compatibility", "full", "read-only", "rejected"):
        if required not in text:
            errors.append(f"Compatibility matrix migration 041 is missing: {required}")


def check_migration_breaking_change(root: Path, errors: list[str]) -> None:
    """Verify migration breaking change migration (042) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "042_migration_breaking_change.sql"
    if not migration.is_file():
        errors.append("Migration breaking change migration 042 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("migration_classification", "classify_migration",
                     "get_breaking_migrations",
                     "compatible", "conditional", "breaking",
                     "pre_migration", "rollback_plan"):
        if required not in text:
            errors.append(f"Migration breaking change migration 042 is missing: {required}")


def check_query_contract_version(root: Path, errors: list[str]) -> None:
    """Verify query contract version migration (043) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "043_query_contract_version.sql"
    if not migration.is_file():
        errors.append("Query contract version migration 043 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("query_contract", "register_query_contract",
                     "deprecate_query_contract", "retire_query_contract",
                     "get_active_query_contract",
                     "active", "deprecated", "retired"):
        if required not in text:
            errors.append(f"Query contract version migration 043 is missing: {required}")


def check_rls_role_migration(root: Path, errors: list[str]) -> None:
    """Verify RLS/Role migration migration (044) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "044_rls_role_migration.sql"
    if not migration.is_file():
        errors.append("RLS/Role migration 044 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("rls_role_migration", "record_rls_role_migration",
                     "get_rls_role_version",
                     "create_role", "grant", "create_policy",
                     "security_definer_grant"):
        if required not in text:
            errors.append(f"RLS/Role migration 044 is missing: {required}")


def check_sqlite_template_release(root: Path, errors: list[str]) -> None:
    """Verify SQLite template release migration (045) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "045_sqlite_template_release.sql"
    if not migration.is_file():
        errors.append("SQLite template release migration 045 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_template_release", "register_sqlite_template",
                     "get_active_sqlite_template", "can_write_sqlite",
                     "template_version", "minimum_writer_version"):
        if required not in text:
            errors.append(f"SQLite template release migration 045 is missing: {required}")


def check_qdrant_contract_version(root: Path, errors: list[str]) -> None:
    """Verify Qdrant contract version migration (046) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "046_qdrant_contract_version.sql"
    if not migration.is_file():
        errors.append("Qdrant contract version migration 046 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_contract", "register_qdrant_contract",
                     "deprecate_qdrant_contract", "retire_qdrant_contract",
                     "get_active_qdrant_contract",
                     "vector_dimension", "distance_metric", "embedding_model"):
        if required not in text:
            errors.append(f"Qdrant contract version migration 046 is missing: {required}")


def check_canary_upgrade(root: Path, errors: list[str]) -> None:
    """Verify canary upgrade migration (047) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "047_canary_upgrade.sql"
    if not migration.is_file():
        errors.append("Canary upgrade migration 047 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("canary_upgrade", "start_canary_upgrade",
                     "complete_canary_upgrade", "promote_canary_to_production",
                     "restoring", "migrating", "certifying",
                     "succeeded", "failed"):
        if required not in text:
            errors.append(f"Canary upgrade migration 047 is missing: {required}")


def check_release_audit(root: Path, errors: list[str]) -> None:
    """Verify release audit migration (048) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "048_release_audit.sql"
    if not migration.is_file():
        errors.append("Release audit migration 048 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("database_release_audit", "start_release_audit",
                     "complete_release_audit", "get_release_audit_history",
                     "schema_hash", "migration_set"):
        if required not in text:
            errors.append(f"Release audit migration 048 is missing: {required}")


def check_roll_forward(root: Path, errors: list[str]) -> None:
    """Verify roll forward migration (049) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "049_roll_forward.sql"
    if not migration.is_file():
        errors.append("Roll forward migration 049 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("roll_forward_migration", "record_roll_forward",
                     "verify_roll_forward", "get_roll_forwards_for",
                     "fixes_migration_id", "corrective_sql"):
        if required not in text:
            errors.append(f"Roll forward migration 049 is missing: {required}")


def check_workload_pool_query_class(root: Path, errors: list[str]) -> None:
    """Verify workload pool + query class migration (032) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "032_workload_pool_query_class.sql"
    if not migration.is_file():
        errors.append("Workload pool/query class migration 032 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("workload_pool_config", "query_class", "apply_query_class",
                     "index", "transport", "audit", "reconcile", "maintenance",
                     "interactive", "index_lookup", "audit_write",
                     "reconcile", "migration"):
        if required not in text:
            errors.append(f"Workload pool/query class migration 032 is missing: {required}")


def check_query_fingerprint(root: Path, errors: list[str]) -> None:
    """Verify query fingerprint migration (033) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "033_query_fingerprint.sql"
    if not migration.is_file():
        errors.append("Query fingerprint migration 033 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("event_history", "archive_audit_events", "partition_threshold"):
        if required not in text:
            errors.append(f"Audit hot/history separation migration 035 is missing: {required}")


def check_wal_checkpoint_monitor(root: Path, errors: list[str]) -> None:
    """Verify WAL checkpoint monitor migration (036) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "036_wal_checkpoint_monitor.sql"
    if not migration.is_file():
        errors.append("WAL checkpoint monitor migration 036 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("wal_checkpoint_snapshot", "record_wal_checkpoint_snapshot",
                     "checkpoint_duration_ms", "wal_rate_mb_per_min"):
        if required not in text:
            errors.append(f"WAL checkpoint monitor migration 036 is missing: {required}")


def check_sqlite_classification(root: Path, errors: list[str]) -> None:
    """Verify SQLite classification migration (037) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "037_sqlite_classification.sql"
    if not migration.is_file():
        errors.append("SQLite classification migration 037 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_database_class", "upsert_sqlite_class",
                     "synchronous_setting", "backup_frequency_seconds",
                     "integrity_check_frequency_seconds", "retention_days",
                     "reconcile_required"):
        if required not in text:
            errors.append(f"SQLite classification migration 037 is missing: {required}")


def check_incremental_reconcile(root: Path, errors: list[str]) -> None:
    """Verify incremental reconcile migration (038) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "038_incremental_reconcile.sql"
    if not migration.is_file():
        errors.append("Incremental reconcile migration 038 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("performance_baseline", "record_baseline",
                     "get_latest_baseline", "compare_baseline",
                     "p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
        if required not in text:
            errors.append(f"Performance baseline migration 039 is missing: {required}")


def check_query_fingerprint_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime query fingerprint module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "query_fingerprint.py"
    if not module.is_file():
        errors.append("Query fingerprint module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("record", "get_hot"):
        if required not in text:
            errors.append(f"Query fingerprint module is missing: {required}")


def check_sqlite_pragma_policy_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime SQLite PRAGMA policy module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "sqlite_pragma_policy.py"
    if not module.is_file():
        errors.append("SQLite PRAGMA policy module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("apply_pragma", "get_pragma_policy", "journal_mode",
                     "foreign_keys", "busy_timeout", "synchronous"):
        if required not in text:
            errors.append(f"SQLite PRAGMA policy module is missing: {required}")


def check_sqlite_classification_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime SQLite classification module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "sqlite_classification.py"
    if not module.is_file():
        errors.append("SQLite classification module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("register", "get_class", "list_by_class"):
        if required not in text:
            errors.append(f"SQLite classification module is missing: {required}")


def check_sqlite_wal_governor_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime SQLite WAL governor module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "sqlite_wal_governor.py"
    if not module.is_file():
        errors.append("SQLite WAL governor module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("check_and_checkpoint", "get_wal_stats", "PASSIVE",
                     "RESTART", "TRUNCATE"):
        if required not in text:
            errors.append(f"SQLite WAL governor module is missing: {required}")


def check_batch_writer_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime batch writer module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "batch_writer.py"
    if not module.is_file():
        errors.append("Batch writer module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("BatchWriter", "add", "flush", "adjust_batch_size"):
        if required not in text:
            errors.append(f"Batch writer module is missing: {required}")


def check_locator_cache_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime locator cache module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "locator_cache.py"
    if not module.is_file():
        errors.append("Locator cache module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("LocatorCache", "get", "put", "invalidate", "stats"):
        if required not in text:
            errors.append(f"Locator cache module is missing: {required}")


def check_prepared_query_catalog_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime prepared query catalog module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "prepared_query_catalog.py"
    if not module.is_file():
        errors.append("Prepared query catalog module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("CATALOG", "get_query", "list_queries",
                     "lookup_resource", "claim_request", "append_audit",
                     "update_index_state", "lookup_locator", "fetch_relationships"):
        if required not in text:
            errors.append(f"Prepared query catalog module is missing: {required}")


def check_performance_baseline_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime performance baseline module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "performance_baseline.py"
    if not module.is_file():
        errors.append("Performance baseline module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("record", "get_latest", "compare"):
        if required not in text:
            errors.append(f"Performance baseline module is missing: {required}")


def check_rebuild_certifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime rebuild certifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "rebuild_certifier.py"
    if not module.is_file():
        errors.append("Rebuild certifier module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("certify", "is_certified"):
        if required not in text:
            errors.append(f"Rebuild certifier is missing: {required}")


def check_watchdog_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime watchdog module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "watchdog.py"
    if not module.is_file():
        errors.append("Watchdog module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("check_long_transactions", "collect_bloat_report",
                     "get_rpo_rto_classes", "get_capacity_thresholds"):
        if required not in text:
            errors.append(f"Watchdog module is missing: {required}")


def check_startup_certifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime startup certifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "startup_certifier.py"
    if not module.is_file():
        errors.append("Startup certifier module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("certify_startup", "is_ready"):
        if required not in text:
            errors.append(f"Startup certifier is missing: {required}")


def check_readonly_domain_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime read-only domain module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "readonly_domain.py"
    if not module.is_file():
        errors.append("Read-only domain module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("set_readonly", "is_readonly"):
        if required not in text:
            errors.append(f"Read-only domain module is missing: {required}")


def check_embedded_browser(root: Path, errors: list[str]) -> None:
    """Verify embedded browser enforcement: no Playwright, modules exist."""
    for module_path in (
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/browser_automation.py",
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/provider_session.py",
        "Standalone tools/vaultly/src/backend/services/vaultly/integration/browser_session.py",
    ):
        full_path = root / module_path
        if full_path.is_file():
            content = full_path.read_text(encoding="utf-8")
            if "from playwright" in content or "import playwright" in content:
                errors.append(f"module still uses Playwright: {module_path}")
            if "async_playwright" in content and "InProcessEmbeddedBrowser" not in content:
                errors.append(f"module still uses async_playwright: {module_path}")

    requirements = root / "main-system" / "requirements.txt"
    if requirements.is_file():
        req_text = requirements.read_text(encoding="utf-8")
        if "playwright" in req_text.lower():
            errors.append("main-system/requirements.txt still depends on playwright")

    pyproject = root / "main-system" / "pyproject.toml"
    if pyproject.is_file():
        py_text = pyproject.read_text(encoding="utf-8")
        if "playwright" in py_text.lower():
            errors.append("main-system/pyproject.toml still depends on playwright")

    embedded_browser = root / "main-system" / "src-ui" / "main" / "embedded-browser.ts"
    if not embedded_browser.is_file():
        errors.append("embedded browser module is missing")

    browser_client = root / "shared-layer" / "src" / "shared_layer" / "embedded_browser_client.py"
    if not browser_client.is_file():
        errors.append("embedded browser client module is missing")
