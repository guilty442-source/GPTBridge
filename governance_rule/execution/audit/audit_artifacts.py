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
        "074_version_lock.sql",
        "075_compatibility_matrix_ext.sql",
        "076_upgrade_classification.sql",
        "077_driver_compatibility_test.sql",
        "078_pg_major_upgrade_rehearsal.sql",
        "079_sqlite_runtime_compat.sql",
        "080_qdrant_contract_compat.sql",
        "081_sbom_dependency_inventory.sql",
        "082_vulnerability_risk.sql",
        "083_dependency_drift.sql",
        "084_offline_bundle.sql",
        "085_release_signature.sql",
        "086_lineage_core.sql",
        "087_security_identity_control.sql",
        "088_recovery_plan.sql",
        "089_recovery_incident.sql",
        "090_recovery_state_machine.sql",
        "091_pg_offline_recovery.sql",
        "092_pg_recovery_verification.sql",
        "093_reconcile_recovery_phase.sql",
        "094_recovery_generation.sql",
        "095_recovery_barrier.sql",
        "096_transport_recovery.sql",
        "097_unknown_commit_resolution.sql",
        "098_lease_recovery.sql",
        "099_sqlite_fallback_freeze.sql",
        "100_recovery_priority.sql",
        "101_qdrant_recovery.sql",
        "102_qdrant_full_rebuild.sql",
        "103_sqlite_single_recovery.sql",
        "104_codex_sqlite_recovery.sql",
        "105_backup_restore_orchestration.sql",
        "106_pitr_boundary.sql",
        "107_recovery_retry_policy.sql",
        "108_recovery_checkpoint.sql",
        "109_recovery_idempotency.sql",
        "110_recovery_safety_fence.sql",
        "111_chaos_drill.sql",
        "112_recovery_certification.sql",
        "113_workflow_operation.sql",
        "114_data_layer_contract.sql",
        "115_dependency_classification.sql",
        "116_startup_phase.sql",
        "117_startup_phase_gate.sql",
        "118_schema_readiness.sql",
        "119_rag_readiness_gate.sql",
        "120_shutdown_phase.sql",
        "121_shutdown_audit.sql",
        "122_unclean_shutdown_detection.sql",
        "123_cache_invalidation_policy.sql",
        "124_data_layer_dependency_graph.sql",
        "125_integration_rule.sql",
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


def check_version_lock(root: Path, errors: list[str]) -> None:
    """Verify version lock migration (074) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "074_version_lock.sql"
    if not migration.is_file():
        errors.append("Version lock migration 074 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("version_lock", "lock_version", "get_version_lock",
                     "get_all_version_locks", "component", "version_string"):
        if required not in text:
            errors.append(f"Version lock migration 074 is missing: {required}")


def check_compatibility_matrix_ext(root: Path, errors: list[str]) -> None:
    """Verify extended compatibility matrix migration (075)."""
    migration = root / "shared-layer" / "migrations" / "075_compatibility_matrix_ext.sql"
    if not migration.is_file():
        errors.append("Compatibility matrix ext migration 075 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("compatibility_matrix_ext", "record_compatibility",
                     "check_combination_allowed", "get_forbidden_combinations",
                     "postgresql_version", "psycopg_version",
                     "sqlite_runtime_version", "qdrant_server_version"):
        if required not in text:
            errors.append(f"Compatibility matrix ext migration 075 is missing: {required}")


def check_upgrade_classification(root: Path, errors: list[str]) -> None:
    """Verify upgrade classification migration (076)."""
    migration = root / "shared-layer" / "migrations" / "076_upgrade_classification.sql"
    if not migration.is_file():
        errors.append("Upgrade classification migration 076 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("upgrade_classification", "classify_upgrade",
                     "get_upgrade_class", "patch", "minor", "major",
                     "required_validation", "allows_unattended"):
        if required not in text:
            errors.append(f"Upgrade classification migration 076 is missing: {required}")


def check_driver_compatibility_test(root: Path, errors: list[str]) -> None:
    """Verify driver compatibility test migration (077)."""
    migration = root / "shared-layer" / "migrations" / "077_driver_compatibility_test.sql"
    if not migration.is_file():
        errors.append("Driver compatibility test migration 077 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("driver_compatibility_test", "record_driver_test",
                     "is_driver_version_verified", "get_failed_driver_tests",
                     "connection_pool", "transaction", "row_factory",
                     "skip_locked"):
        if required not in text:
            errors.append(f"Driver compatibility test migration 077 is missing: {required}")


def check_pg_major_upgrade_rehearsal(root: Path, errors: list[str]) -> None:
    """Verify PG major upgrade rehearsal migration (078)."""
    migration = root / "shared-layer" / "migrations" / "078_pg_major_upgrade_rehearsal.sql"
    if not migration.is_file():
        errors.append("PG major upgrade rehearsal migration 078 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("pg_major_upgrade_rehearsal", "start_pg_rehearsal",
                     "advance_pg_rehearsal", "get_rehearsal_summary",
                     "migration_check", "rls_check", "transport_test",
                     "reconcile_test", "performance_baseline"):
        if required not in text:
            errors.append(f"PG major upgrade rehearsal migration 078 is missing: {required}")


def check_sqlite_runtime_compat(root: Path, errors: list[str]) -> None:
    """Verify SQLite runtime compat migration (079)."""
    migration = root / "shared-layer" / "migrations" / "079_sqlite_runtime_compat.sql"
    if not migration.is_file():
        errors.append("SQLite runtime compat migration 079 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_runtime_compat", "record_sqlite_runtime_compat",
                     "check_sqlite_runtime_compat",
                     "python_version", "sqlite_library_version",
                     "fts5_available", "wal_mode_available", "json1_available"):
        if required not in text:
            errors.append(f"SQLite runtime compat migration 079 is missing: {required}")


def check_qdrant_contract_compat(root: Path, errors: list[str]) -> None:
    """Verify Qdrant contract compat migration (080)."""
    migration = root / "shared-layer" / "migrations" / "080_qdrant_contract_compat.sql"
    if not migration.is_file():
        errors.append("Qdrant contract compat migration 080 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_contract_compat", "record_qdrant_compat",
                     "is_qdrant_upgrade_safe",
                     "collection_schema", "payload_filter", "snapshot_format",
                     "index_config", "client_api", "point_id_behavior"):
        if required not in text:
            errors.append(f"Qdrant contract compat migration 080 is missing: {required}")


def check_sbom_dependency_inventory(root: Path, errors: list[str]) -> None:
    """Verify SBOM dependency inventory migration (081)."""
    migration = root / "shared-layer" / "migrations" / "081_sbom_dependency_inventory.sql"
    if not migration.is_file():
        errors.append("SBOM dependency inventory migration 081 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sbom_dependency_inventory", "record_sbom_entry",
                     "verify_sbom_entry", "get_sbom_for_release",
                     "component", "component_type", "version",
                     "source", "source_hash", "install_path"):
        if required not in text:
            errors.append(f"SBOM dependency inventory migration 081 is missing: {required}")


def check_vulnerability_risk(root: Path, errors: list[str]) -> None:
    """Verify vulnerability risk migration (082)."""
    migration = root / "shared-layer" / "migrations" / "082_vulnerability_risk.sql"
    if not migration.is_file():
        errors.append("Vulnerability risk migration 082 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("vulnerability_risk", "record_vulnerability",
                     "resolve_vulnerability", "get_critical_vulnerabilities",
                     "critical_security", "important",
                     "compatible_maintenance", "optional",
                     "immediate_upgrade", "scheduled_upgrade"):
        if required not in text:
            errors.append(f"Vulnerability risk migration 082 is missing: {required}")


def check_dependency_drift(root: Path, errors: list[str]) -> None:
    """Verify dependency drift migration (083)."""
    migration = root / "shared-layer" / "migrations" / "083_dependency_drift.sql"
    if not migration.is_file():
        errors.append("Dependency drift migration 083 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("dependency_drift", "record_dependency_drift",
                     "resolve_dependency_drift", "get_unverified_dependencies",
                     "UNVERIFIED_DEPENDENCY", "MISMATCH", "VERIFIED",
                     "expected_version", "installed_version"):
        if required not in text:
            errors.append(f"Dependency drift migration 083 is missing: {required}")


def check_offline_bundle(root: Path, errors: list[str]) -> None:
    """Verify offline bundle migration (084)."""
    migration = root / "shared-layer" / "migrations" / "084_offline_bundle.sql"
    if not migration.is_file():
        errors.append("Offline bundle migration 084 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("offline_bundle", "register_offline_bundle",
                     "verify_offline_bundle", "get_offline_bundle",
                     "component", "version", "package_type",
                     "storage_locator", "file_hash"):
        if required not in text:
            errors.append(f"Offline bundle migration 084 is missing: {required}")


def check_release_signature(root: Path, errors: list[str]) -> None:
    """Verify release signature migration (085)."""
    migration = root / "shared-layer" / "migrations" / "085_release_signature.sql"
    if not migration.is_file():
        errors.append("Release signature migration 085 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("release_signature", "sign_release",
                     "verify_release_signature", "get_latest_signature",
                     "bundle_hash", "component_count", "component_hashes",
                     "tamper_state"):
        if required not in text:
            errors.append(f"Release signature migration 085 is missing: {required}")


def check_dependency_governor_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime dependency governor module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "dependency_governor.py"
    if not module.is_file():
        errors.append("Dependency governor module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("compute_bundle_hash", "lock_version", "get_version_lock",
                     "record_compatibility", "check_combination_allowed",
                     "classify_upgrade", "get_upgrade_class",
                     "record_driver_test", "is_driver_version_verified",
                     "start_pg_rehearsal", "advance_pg_rehearsal",
                     "record_sqlite_runtime_compat", "record_qdrant_compat",
                     "record_sbom_entry", "record_vulnerability",
                     "record_dependency_drift", "register_offline_bundle",
                     "sign_release", "verify_release_signature"):
        if required not in text:
            errors.append(f"Dependency governor module is missing: {required}")


def check_recovery_plan(root: Path, errors: list[str]) -> None:
    """Verify recovery plan migration (088) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "088_recovery_plan.sql"
    if not migration.is_file():
        errors.append("Recovery plan migration 088 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_plan", "register_recovery_plan",
                     "certify_recovery_plan", "activate_recovery_plan",
                     "get_active_recovery_plan",
                     "incident_type", "steps", "verification_rules"):
        if required not in text:
            errors.append(f"Recovery plan migration 088 is missing: {required}")


def check_recovery_incident(root: Path, errors: list[str]) -> None:
    """Verify recovery incident migration (089)."""
    migration = root / "shared-layer" / "migrations" / "089_recovery_incident.sql"
    if not migration.is_file():
        errors.append("Recovery incident migration 089 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_incident", "open_recovery_incident",
                     "advance_incident_status", "get_active_incidents"):
        if required not in text:
            errors.append(f"Recovery incident migration 089 is missing: {required}")


def check_recovery_state_machine(root: Path, errors: list[str]) -> None:
    """Verify recovery state machine migration (090)."""
    migration = root / "shared-layer" / "migrations" / "090_recovery_state_machine.sql"
    if not migration.is_file():
        errors.append("Recovery state machine migration 090 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_state_machine", "recovery_state_transition",
                     "transition_recovery_state", "get_current_recovery_state",
                     "HEALTHY", "DEGRADED", "RECOVERING", "QUARANTINED"):
        if required not in text:
            errors.append(f"Recovery state machine migration 090 is missing: {required}")


def check_pg_offline_recovery(root: Path, errors: list[str]) -> None:
    """Verify PG offline recovery migration (091)."""
    migration = root / "shared-layer" / "migrations" / "091_pg_offline_recovery.sql"
    if not migration.is_file():
        errors.append("PG offline recovery migration 091 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("pg_offline_recovery", "confirm_pg_failure",
                     "enter_degraded_mode", "fallback_status"):
        if required not in text:
            errors.append(f"PG offline recovery migration 091 is missing: {required}")


def check_pg_recovery_verification(root: Path, errors: list[str]) -> None:
    """Verify PG recovery verification migration (092)."""
    migration = root / "shared-layer" / "migrations" / "092_pg_recovery_verification.sql"
    if not migration.is_file():
        errors.append("PG recovery verification migration 092 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("pg_recovery_verification", "record_pg_recovery_verification",
                     "is_pg_recoverable", "connection_ok", "schema_version_ok",
                     "rls_ok", "audit_ok", "transport_ok", "integrity_ok",
                     "generation_ok", "overall_recoverable"):
        if required not in text:
            errors.append(f"PG recovery verification migration 092 is missing: {required}")


def check_reconcile_recovery_phase(root: Path, errors: list[str]) -> None:
    """Verify reconcile recovery phase migration (093)."""
    migration = root / "shared-layer" / "migrations" / "093_reconcile_recovery_phase.sql"
    if not migration.is_file():
        errors.append("Reconcile recovery phase migration 093 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("reconcile_recovery_phase", "start_reconcile_recovery",
                     "advance_reconcile_recovery", "is_reconcile_complete"):
        if required not in text:
            errors.append(f"Reconcile recovery phase migration 093 is missing: {required}")


def check_recovery_generation(root: Path, errors: list[str]) -> None:
    """Verify recovery generation migration (094)."""
    migration = root / "shared-layer" / "migrations" / "094_recovery_generation.sql"
    if not migration.is_file():
        errors.append("Recovery generation migration 094 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_generation", "create_recovery_generation",
                     "get_current_generation", "degraded", "recovered"):
        if required not in text:
            errors.append(f"Recovery generation migration 094 is missing: {required}")


def check_recovery_barrier(root: Path, errors: list[str]) -> None:
    """Verify recovery barrier migration (095)."""
    migration = root / "shared-layer" / "migrations" / "095_recovery_barrier.sql"
    if not migration.is_file():
        errors.append("Recovery barrier migration 095 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_barrier", "raise_recovery_barrier",
                     "release_recovery_barrier", "is_recovery_barrier_active",
                     "RECOVERING_READ_ONLY"):
        if required not in text:
            errors.append(f"Recovery barrier migration 095 is missing: {required}")


def check_transport_recovery(root: Path, errors: list[str]) -> None:
    """Verify transport recovery migration (096)."""
    migration = root / "shared-layer" / "migrations" / "096_transport_recovery.sql"
    if not migration.is_file():
        errors.append("Transport recovery migration 096 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("transport_recovery", "register_unknown_commit",
                     "resolve_commit_state", "get_unknown_commits",
                     "idempotency_key", "COMMITTED", "NOT_COMMITTED", "UNKNOWN"):
        if required not in text:
            errors.append(f"Transport recovery migration 096 is missing: {required}")


def check_lease_recovery(root: Path, errors: list[str]) -> None:
    """Verify lease recovery migration (098)."""
    migration = root / "shared-layer" / "migrations" / "098_lease_recovery.sql"
    if not migration.is_file():
        errors.append("Lease recovery migration 098 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("lease_recovery", "check_lease_expiry",
                     "reclaim_lease", "get_expired_leases",
                     "lease_until", "worker_generation"):
        if required not in text:
            errors.append(f"Lease recovery migration 098 is missing: {required}")


def check_sqlite_fallback_freeze(root: Path, errors: list[str]) -> None:
    """Verify SQLite fallback freeze migration (099)."""
    migration = root / "shared-layer" / "migrations" / "099_sqlite_fallback_freeze.sql"
    if not migration.is_file():
        errors.append("SQLite fallback freeze migration 099 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_fallback_freeze", "transition_fallback_state",
                     "get_fallback_state",
                     "fallback_open", "fallback_draining",
                     "fallback_frozen", "fallback_closed"):
        if required not in text:
            errors.append(f"SQLite fallback freeze migration 099 is missing: {required}")


def check_qdrant_recovery(root: Path, errors: list[str]) -> None:
    """Verify Qdrant recovery migration (101)."""
    migration = root / "shared-layer" / "migrations" / "101_qdrant_recovery.sql"
    if not migration.is_file():
        errors.append("Qdrant recovery migration 101 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_recovery", "start_qdrant_recovery",
                     "update_qdrant_recovery", "indexing_backlog_count"):
        if required not in text:
            errors.append(f"Qdrant recovery migration 101 is missing: {required}")


def check_qdrant_full_rebuild(root: Path, errors: list[str]) -> None:
    """Verify Qdrant full rebuild migration (102)."""
    migration = root / "shared-layer" / "migrations" / "102_qdrant_full_rebuild.sql"
    if not migration.is_file():
        errors.append("Qdrant full rebuild migration 102 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("qdrant_full_rebuild", "start_qdrant_full_rebuild",
                     "advance_qdrant_rebuild", "collection_generation",
                     "old_collection_retired"):
        if required not in text:
            errors.append(f"Qdrant full rebuild migration 102 is missing: {required}")


def check_recovery_checkpoint(root: Path, errors: list[str]) -> None:
    """Verify recovery checkpoint migration (108)."""
    migration = root / "shared-layer" / "migrations" / "108_recovery_checkpoint.sql"
    if not migration.is_file():
        errors.append("Recovery checkpoint migration 108 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_checkpoint", "save_recovery_checkpoint",
                     "resume_recovery_checkpoint", "get_latest_checkpoint",
                     "current_phase", "last_processed_revision", "batch_cursor"):
        if required not in text:
            errors.append(f"Recovery checkpoint migration 108 is missing: {required}")


def check_recovery_idempotency(root: Path, errors: list[str]) -> None:
    """Verify recovery idempotency migration (109)."""
    migration = root / "shared-layer" / "migrations" / "109_recovery_idempotency.sql"
    if not migration.is_file():
        errors.append("Recovery idempotency migration 109 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_idempotency", "check_or_mark_idempotent",
                     "mark_idempotent_complete", "operation_key"):
        if required not in text:
            errors.append(f"Recovery idempotency migration 109 is missing: {required}")


def check_recovery_safety_fence(root: Path, errors: list[str]) -> None:
    """Verify recovery safety fence migration (110)."""
    migration = root / "shared-layer" / "migrations" / "110_recovery_safety_fence.sql"
    if not migration.is_file():
        errors.append("Recovery safety fence migration 110 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_safety_fence", "check_safety_fence",
                     "record_fence_block", "drop_authoritative_database",
                     "truncate_official_data", "rewrite_governance_codex",
                     "change_rls_policy", "grant_elevated_role"):
        if required not in text:
            errors.append(f"Recovery safety fence migration 110 is missing: {required}")


def check_chaos_drill(root: Path, errors: list[str]) -> None:
    """Verify chaos drill migration (111)."""
    migration = root / "shared-layer" / "migrations" / "111_chaos_drill.sql"
    if not migration.is_file():
        errors.append("Chaos drill migration 111 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("chaos_drill", "start_chaos_drill", "complete_chaos_drill",
                     "no_authority_inversion", "no_duplicate_write",
                     "no_lost_commit", "no_silent_conflict",
                     "no_uncontrolled_retry"):
        if required not in text:
            errors.append(f"Chaos drill migration 111 is missing: {required}")


def check_recovery_certification(root: Path, errors: list[str]) -> None:
    """Verify recovery certification migration (112)."""
    migration = root / "shared-layer" / "migrations" / "112_recovery_certification.sql"
    if not migration.is_file():
        errors.append("Recovery certification migration 112 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("recovery_certification", "record_recovery_certification",
                     "certify_recovery_plan_v2", "is_recovery_plan_certified",
                     "integrity_result", "reconcile_result", "audit_result",
                     "certification_status", "CERTIFIED"):
        if required not in text:
            errors.append(f"Recovery certification migration 112 is missing: {required}")


def check_recovery_orchestrator_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime recovery orchestrator module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "recovery_orchestrator.py"
    if not module.is_file():
        errors.append("Recovery orchestrator module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("register_recovery_plan", "open_recovery_incident",
                     "transition_recovery_state", "create_recovery_generation",
                     "get_current_generation", "start_pg_offline_recovery",
                     "confirm_pg_failure", "record_pg_recovery_verification",
                     "is_pg_recoverable", "register_unknown_commit",
                     "resolve_commit_state", "check_lease_expiry",
                     "reclaim_lease", "raise_recovery_barrier",
                     "is_recovery_barrier_active", "release_recovery_barrier",
                     "save_recovery_checkpoint", "check_or_mark_idempotent",
                     "mark_idempotent_complete", "check_safety_fence",
                     "record_fence_block", "is_recovery_plan_certified",
                     "start_chaos_drill", "complete_chaos_drill"):
        if required not in text:
            errors.append(f"Recovery orchestrator module is missing: {required}")


def check_data_layer_contract(root: Path, errors: list[str]) -> None:
    """Verify data layer contract migration (114)."""
    migration = root / "shared-layer" / "migrations" / "114_data_layer_contract.sql"
    if not migration.is_file():
        errors.append("Data layer contract migration 114 is missing")
        return
    text = migration.read_text(encoding="utf-8")
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
    text = migration.read_text(encoding="utf-8")
    for required in ("dependency_classification", "authority", "required",
                     "degradable", "optional", "classify_dependency",
                     "get_dependency_classification",
                     "postgresql", "sqlite_codex", "qdrant"):
        if required not in text:
            errors.append(f"Dependency classification migration 115 is missing: {required}")


def check_startup_phase(root: Path, errors: list[str]) -> None:
    """Verify startup phase migration (116)."""
    migration = root / "shared-layer" / "migrations" / "116_startup_phase.sql"
    if not migration.is_file():
        errors.append("Startup phase migration 116 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("startup_phase", "BOOTSTRAP", "GOVERNANCE_VALIDATED",
                     "DATABASE_FOUNDATION_READY", "CENTRAL_AUTHORITY_READY",
                     "PRIVATE_STATE_READY", "SEMANTIC_INDEX_READY",
                     "RECOVERY_READY", "READ_MODELS_READY", "CORE_READY",
                     "get_startup_order"):
        if required not in text:
            errors.append(f"Startup phase migration 116 is missing: {required}")


def check_startup_phase_gate(root: Path, errors: list[str]) -> None:
    """Verify startup phase gate migration (117)."""
    migration = root / "shared-layer" / "migrations" / "117_startup_phase_gate.sql"
    if not migration.is_file():
        errors.append("Startup phase gate migration 117 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("startup_phase_gate", "register_startup_gate",
                     "set_gate_result", "is_phase_complete",
                     "can_enable_write", "governance_ready",
                     "security_ready", "authority_ready", "audit_ready"):
        if required not in text:
            errors.append(f"Startup phase gate migration 117 is missing: {required}")


def check_schema_readiness(root: Path, errors: list[str]) -> None:
    """Verify schema readiness migration (118)."""
    migration = root / "shared-layer" / "migrations" / "118_schema_readiness.sql"
    if not migration.is_file():
        errors.append("Schema readiness migration 118 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("schema_readiness", "set_schema_readiness",
                     "is_schema_ready", "is_pg_certified",
                     "is_audit_writable", "can_enable_business_write",
                     "gptbridge_index", "gptbridge_transport",
                     "gptbridge_audit", "gptbridge_rag", "gptbridge_identity"):
        if required not in text:
            errors.append(f"Schema readiness migration 118 is missing: {required}")


def check_rag_readiness_gate(root: Path, errors: list[str]) -> None:
    """Verify RAG readiness gate migration (119)."""
    migration = root / "shared-layer" / "migrations" / "119_rag_readiness_gate.sql"
    if not migration.is_file():
        errors.append("RAG readiness gate migration 119 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("rag_readiness_gate", "evaluate_rag_readiness",
                     "is_rag_ready", "pg_rag_metadata_ready",
                     "qdrant_ready", "metadata_authority_wired",
                     "collection_contract_valid"):
        if required not in text:
            errors.append(f"RAG readiness gate migration 119 is missing: {required}")


def check_shutdown_phase(root: Path, errors: list[str]) -> None:
    """Verify shutdown phase migration (120)."""
    migration = root / "shared-layer" / "migrations" / "120_shutdown_phase.sql"
    if not migration.is_file():
        errors.append("Shutdown phase migration 120 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("shutdown_phase", "STOP_ACCEPTING_NEW_WORK",
                     "DRAIN_TRANSPORT", "FLUSH_AUDIT",
                     "CLOSE_QDRANT_CLIENT", "CLOSE_SQLITE",
                     "CLOSE_POSTGRES_POOLS", "get_shutdown_order"):
        if required not in text:
            errors.append(f"Shutdown phase migration 120 is missing: {required}")


def check_shutdown_audit(root: Path, errors: list[str]) -> None:
    """Verify shutdown audit migration (121)."""
    migration = root / "shared-layer" / "migrations" / "121_shutdown_audit.sql"
    if not migration.is_file():
        errors.append("Shutdown audit migration 121 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("shutdown_audit", "start_shutdown_audit",
                     "complete_shutdown_audit",
                     "was_last_shutdown_graceful",
                     "shutdown_status", "graceful", "unclean"):
        if required not in text:
            errors.append(f"Shutdown audit migration 121 is missing: {required}")


def check_unclean_shutdown_detection(root: Path, errors: list[str]) -> None:
    """Verify unclean shutdown detection migration (122)."""
    migration = root / "shared-layer" / "migrations" / "122_unclean_shutdown_detection.sql"
    if not migration.is_file():
        errors.append("Unclean shutdown detection migration 122 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("unclean_shutdown_detection", "detect_unclean_shutdown",
                     "mark_unclean_step_done",
                     "is_unclean_recovery_complete",
                     "transport_lease_recovery",
                     "unknown_commit_verification",
                     "sqlite_wal_verification"):
        if required not in text:
            errors.append(f"Unclean shutdown detection migration 122 is missing: {required}")


def check_cache_invalidation_policy(root: Path, errors: list[str]) -> None:
    """Verify cache invalidation policy migration (123)."""
    migration = root / "shared-layer" / "migrations" / "123_cache_invalidation_policy.sql"
    if not migration.is_file():
        errors.append("Cache invalidation policy migration 123 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("cache_invalidation_policy", "register_cache_policy",
                     "should_invalidate_cache",
                     "check_generation_compatible",
                     "check_revision_compatible", "check_ttl_valid"):
        if required not in text:
            errors.append(f"Cache invalidation policy migration 123 is missing: {required}")


def check_dependency_graph(root: Path, errors: list[str]) -> None:
    """Verify dependency graph migration (124)."""
    migration = root / "shared-layer" / "migrations" / "124_data_layer_dependency_graph.sql"
    if not migration.is_file():
        errors.append("Dependency graph migration 124 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("data_layer_dependency_graph", "get_dependencies",
                     "get_dependents", "governance_codex",
                     "identity_permission", "postgresql",
                     "structured_authority", "semantic_canonical",
                     "bounded_local_state", "non_canonical"):
        if required not in text:
            errors.append(f"Dependency graph migration 124 is missing: {required}")


def check_integration_rule(root: Path, errors: list[str]) -> None:
    """Verify integration rule migration (125)."""
    migration = root / "shared-layer" / "migrations" / "125_integration_rule.sql"
    if not migration.is_file():
        errors.append("Integration rule migration 125 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("integration_rule", "get_integration_rules",
                     "check_integration_rule",
                     "central structured authority",
                     "canonical semantic index",
                     "durable workflow",
                     "bounded", "rebuildable"):
        if required not in text:
            errors.append(f"Integration rule migration 125 is missing: {required}")


def check_data_layer_contract_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime data layer contract module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "data_layer_contract.py"
    if not module.is_file():
        errors.append("Data layer contract module is missing")
        return
    text = module.read_text(encoding="utf-8")
    for required in ("register_data_layer_contract", "activate_data_layer_contract",
                     "get_active_data_layer_contract", "classify_dependency",
                     "get_startup_order", "get_shutdown_order",
                     "register_startup_gate", "set_gate_result",
                     "is_phase_complete", "can_enable_write",
                     "set_schema_readiness", "is_schema_ready",
                     "is_pg_certified", "can_enable_business_write",
                     "evaluate_rag_readiness", "is_rag_ready",
                     "start_shutdown_audit", "complete_shutdown_audit",
                     "was_last_shutdown_graceful", "detect_unclean_shutdown",
                     "mark_unclean_step_done", "is_unclean_recovery_complete",
                     "register_cache_policy", "should_invalidate_cache",
                     "get_dependencies", "get_dependents",
                     "get_integration_rules", "check_integration_rule"):
        if required not in text:
            errors.append(f"Data layer contract module is missing: {required}")


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
