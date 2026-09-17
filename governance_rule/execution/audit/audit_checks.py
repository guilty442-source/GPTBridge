"""Orchestrator for the governance runtime audit."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Final

from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)

from .audit_artifacts import (
    check_authority_marker,
    check_codex_consistency,
    check_contract_handshake,
    check_data_lineage,
    check_ddl_audit,
    check_deletion_coordinator,
    check_embedded_browser,
    check_generation_fence_helper,
    check_git_tiers,
    check_metadata_contract,
    check_orphan_scanner,
    check_permission_snapshot,
    check_permission_snapshot_helper,
    check_provenance_helper,
    check_readonly_domain_module,
    check_readonly_domain_startup_cert,
    check_rebuild_certification,
    check_rebuild_certifier_module,
    check_reconcile_modules,
    check_schema_ownership_lock,
    check_slo_metrics,
    check_sql_migrations,
    check_sqlite_template,
    check_sqlite_generation_fence,
    check_startup_certifier_module,
    check_two_stage_deletion,
    check_watchdog_bloat_rpo_rto,
    check_watchdog_module,
    check_workload_class,
    check_write_provenance,
    # Phase E
    check_audit_hot_history_separation,
    check_batch_writer_module,
    check_incremental_reconcile,
    check_locator_cache_module,
    check_performance_baseline,
    check_performance_baseline_module,
    check_prepared_query_catalog_module,
    check_query_fingerprint,
    check_query_fingerprint_module,
    check_sqlite_classification,
    check_sqlite_classification_module,
    check_sqlite_pragma_policy_module,
    check_sqlite_wal_governor_module,
    check_transport_hot_path_index,
    check_wal_checkpoint_monitor,
    check_workload_pool_query_class,
    # Phase F
    check_canary_upgrade,
    check_compatibility_matrix,
    check_database_release_manifest,
    check_migration_breaking_change,
    check_qdrant_contract_version,
    check_query_contract_version,
    check_release_audit,
    check_release_manifest_file,
    check_release_manifest_module,
    check_rls_role_migration,
    check_roll_forward,
    check_sqlite_template_release,
    # Phase G
    check_archive_catalog,
    check_archive_restore_test,
    check_archive_versioning,
    check_audit_retention_layering,
    check_capacity_quota,
    check_dependency_check,
    check_lifecycle_manager_module,
    check_purge_audit,
    check_purge_queue,
    check_qdrant_vector_lifecycle,
    check_retention_hold,
    check_sqlite_per_class_retention,
    check_transport_retention,
    check_unified_lifecycle_state,
    # Phase H
    check_audit_hash_chain,
    check_reconcile_batch_digest,
    check_resource_content_hash,
    check_sqlite_database_digest,
    check_qdrant_integrity_mapping,
    check_merkle_root,
    check_integrity_snapshot,
    check_restore_verification,
    check_tamper_state,
    check_fail_closed,
    check_integrity_verifier_module,
    # Phase I
    check_version_lock,
    check_compatibility_matrix_ext,
    check_upgrade_classification,
    check_driver_compatibility_test,
    check_pg_major_upgrade_rehearsal,
    check_sqlite_runtime_compat,
    check_qdrant_contract_compat,
    check_sbom_dependency_inventory,
    check_vulnerability_risk,
    check_dependency_drift,
    check_offline_bundle,
    check_release_signature,
    check_dependency_governor_module,
    # Phase J
    check_recovery_plan,
    check_recovery_incident,
    check_recovery_state_machine,
    check_pg_offline_recovery,
    check_pg_recovery_verification,
    check_reconcile_recovery_phase,
    check_recovery_generation,
    check_recovery_barrier,
    check_transport_recovery,
    check_lease_recovery,
    check_sqlite_fallback_freeze,
    check_qdrant_recovery,
    check_qdrant_full_rebuild,
    check_recovery_checkpoint,
    check_recovery_idempotency,
    check_recovery_safety_fence,
    check_chaos_drill,
    check_recovery_certification,
    check_recovery_orchestrator_module,
    # Phase K
    check_data_layer_contract,
    check_dependency_classification,
    check_startup_phase,
    check_startup_phase_gate,
    check_schema_readiness,
    check_rag_readiness_gate,
    check_shutdown_phase,
    check_shutdown_audit,
    check_unclean_shutdown_detection,
    check_cache_invalidation_policy,
    check_dependency_graph,
    check_integration_rule,
    check_data_layer_contract_module,
)
from .audit_codex_integrity import (
    check_codex_mirror_quality,
    check_codex_text_integrity,
)
from .audit_authority import (
    check_architecture_sources,
    check_identity_permissions,
    check_main_system_source,
    check_authority_policy,
    check_repair_policy,
    check_shared_layer_policy,
    check_shared_layer_structure,
    check_third_party_inventory,
    check_tool_isolation_hardening,
)
from .audit_activation import check_activation_states
from .audit_architecture import check_architecture_registry
from .audit_directories import check_directory_audit
from .audit_formal_rules import (
    check_formal_rules,
    check_implementation_obligations,
)
from .audit_manifests import (
    check_tool_identity_registration,
    check_tool_manifests,
)
from .audit_protected import (
    check_forbidden_legacy,
    check_protected_sources,
)
from .audit_runtime_contracts import check_runtime_contracts
from .audit_self_health import _verify_self_health_test_files

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Governor runtime-budget amendment (staged 2026-09-17): the ENTIRE
# governance audit flow — every check, the self-health test-file barrier and
# the merged verdict — must finish inside a hard 30-second wall-clock
# deadline.  Exceeding it is fail-closed (an over-budget audit never passes).
AUDIT_FLOW_BUDGET_SECONDS: Final[float] = 30.0


def audit_flow_budget_error(elapsed_seconds: float) -> str | None:
    """Return the budget violation error for one completed audit flow.

    The clock is the caller's monotonic high-resolution elapsed time; an
    elapsed time strictly greater than the budget is a failure.  No
    configuration path may extend the budget.
    """
    if elapsed_seconds > AUDIT_FLOW_BUDGET_SECONDS:
        return (
            f"audit flow budget exceeded: {elapsed_seconds:.3f}s > "
            f"{AUDIT_FLOW_BUDGET_SECONDS:.0f}s"
        )
    return None


def _audit_workers(check_count: int) -> int:
    """Pick the audit thread count.

    The checks are a mix of I/O waits and pure-Python scanning; past four
    workers the GIL hand-off overhead outweighs the parallelism (measured
    on the 16-thread build machine: 8 workers were ~25% slower than 4).
    ``GPTBRIDGE_AUDIT_WORKERS`` overrides the default.
    """
    override = str(os.environ.get("GPTBRIDGE_AUDIT_WORKERS", "")).strip()
    if override.isdigit() and int(override) > 0:
        return max(1, min(int(override), check_count))
    return max(1, min(4, check_count))


def _sweep_stale_temp_files(directory: Path, max_age_seconds: float = 3600.0) -> None:
    """Remove abandoned atomic-write temp files left by crashed audits."""
    now = time.time()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(".tmp"):
            continue
        try:
            if now - entry.stat().st_mtime > max_age_seconds:
                os.unlink(entry.path)
        except OSError:
            continue


def _collect(check: Callable[[Path, list[str]], None], root: Path) -> list[str]:
    errors: list[str] = []
    check(root, errors)
    return errors


def _manifest_pair(root: Path) -> list[str]:
    """Manifest checks are dependent — identity registration needs the
    manifest tool ids — so they run as one task."""
    errors: list[str] = []
    manifest_tool_ids, _ = check_tool_manifests(root, errors)
    check_tool_identity_registration(root, errors, manifest_tool_ids)
    return errors


def audit_runtime_governance(
    project_root: Path = PROJECT_ROOT,
    *,
    include_self_health: bool = True,
) -> list[str]:
    """Run governance checks and return errors.

    Checks are independent (each opens its own file handles and codex
    connections) so they run concurrently; the merged error list preserves
    the original declaration order, keeping results deterministic.
    Startup may omit subprocess-based test collection; release and explicit
    audits retain the complete self-health barrier by default.  Every call
    performs the full read-only checks — no result is ever reused.

    The whole flow is measured on a monotonic clock and must finish within
    ``AUDIT_FLOW_BUDGET_SECONDS`` (the governor's hard 30-second budget); an
    over-budget flow returns a fail-closed budget error in addition to any
    check findings.
    """
    started = time.monotonic()
    root = project_root.resolve()
    _sweep_stale_temp_files(Path(__file__).resolve().parent)

    errors: list[str] = []

    checks: list[Callable[[Path], list[str]]] = [
        source_ownership_errors,
        lambda r: _collect(check_authority_policy, r),
        lambda r: _collect(check_architecture_sources, r),
        lambda r: _collect(check_third_party_inventory, r),
        lambda r: _collect(check_shared_layer_policy, r),
        lambda r: _collect(check_repair_policy, r),
        lambda r: _collect(check_protected_sources, r),
        lambda r: _collect(check_forbidden_legacy, r),
        lambda r: _collect(check_runtime_contracts, r),
        lambda r: _collect(check_identity_permissions, r),
        lambda r: _collect(check_main_system_source, r),
        lambda r: _collect(check_shared_layer_structure, r),
        lambda r: _collect(check_tool_isolation_hardening, r),
        _manifest_pair,
        lambda r: _collect(check_codex_consistency, r),
        lambda r: _collect(check_codex_text_integrity, r),
        lambda r: _collect(check_codex_mirror_quality, r),
        lambda r: _collect(check_architecture_registry, r),
        lambda r: _collect(check_directory_audit, r),
        lambda r: _collect(check_activation_states, r),
        lambda r: _collect(check_formal_rules, r),
        lambda r: _collect(check_implementation_obligations, r),
        lambda r: _collect(check_git_tiers, r),
        lambda r: _collect(check_metadata_contract, r),
        lambda r: _collect(check_reconcile_modules, r),
        lambda r: _collect(check_sql_migrations, r),
        lambda r: _collect(check_sqlite_template, r),
        lambda r: _collect(check_data_lineage, r),
        lambda r: _collect(check_authority_marker, r),
        lambda r: _collect(check_write_provenance, r),
        lambda r: _collect(check_provenance_helper, r),
        lambda r: _collect(check_permission_snapshot, r),
        lambda r: _collect(check_schema_ownership_lock, r),
        lambda r: _collect(check_ddl_audit, r),
        lambda r: _collect(check_contract_handshake, r),
        lambda r: _collect(check_permission_snapshot_helper, r),
        lambda r: _collect(check_sqlite_generation_fence, r),
        lambda r: _collect(check_workload_class, r),
        lambda r: _collect(check_two_stage_deletion, r),
        lambda r: _collect(check_orphan_scanner, r),
        lambda r: _collect(check_deletion_coordinator, r),
        lambda r: _collect(check_generation_fence_helper, r),
        lambda r: _collect(check_rebuild_certification, r),
        lambda r: _collect(check_watchdog_bloat_rpo_rto, r),
        lambda r: _collect(check_readonly_domain_startup_cert, r),
        lambda r: _collect(check_slo_metrics, r),
        lambda r: _collect(check_rebuild_certifier_module, r),
        lambda r: _collect(check_watchdog_module, r),
        lambda r: _collect(check_startup_certifier_module, r),
        lambda r: _collect(check_readonly_domain_module, r),
        lambda r: _collect(check_workload_pool_query_class, r),
        lambda r: _collect(check_query_fingerprint, r),
        lambda r: _collect(check_transport_hot_path_index, r),
        lambda r: _collect(check_audit_hot_history_separation, r),
        lambda r: _collect(check_wal_checkpoint_monitor, r),
        lambda r: _collect(check_sqlite_classification, r),
        lambda r: _collect(check_incremental_reconcile, r),
        lambda r: _collect(check_performance_baseline, r),
        lambda r: _collect(check_query_fingerprint_module, r),
        lambda r: _collect(check_sqlite_pragma_policy_module, r),
        lambda r: _collect(check_sqlite_classification_module, r),
        lambda r: _collect(check_sqlite_wal_governor_module, r),
        lambda r: _collect(check_batch_writer_module, r),
        lambda r: _collect(check_locator_cache_module, r),
        lambda r: _collect(check_prepared_query_catalog_module, r),
        lambda r: _collect(check_performance_baseline_module, r),
        lambda r: _collect(check_database_release_manifest, r),
        lambda r: _collect(check_release_manifest_file, r),
        lambda r: _collect(check_release_manifest_module, r),
        lambda r: _collect(check_compatibility_matrix, r),
        lambda r: _collect(check_migration_breaking_change, r),
        lambda r: _collect(check_query_contract_version, r),
        lambda r: _collect(check_rls_role_migration, r),
        lambda r: _collect(check_sqlite_template_release, r),
        lambda r: _collect(check_qdrant_contract_version, r),
        lambda r: _collect(check_canary_upgrade, r),
        lambda r: _collect(check_release_audit, r),
        lambda r: _collect(check_roll_forward, r),
        lambda r: _collect(check_unified_lifecycle_state, r),
        lambda r: _collect(check_transport_retention, r),
        lambda r: _collect(check_audit_retention_layering, r),
        lambda r: _collect(check_sqlite_per_class_retention, r),
        lambda r: _collect(check_qdrant_vector_lifecycle, r),
        lambda r: _collect(check_purge_queue, r),
        lambda r: _collect(check_archive_catalog, r),
        lambda r: _collect(check_archive_versioning, r),
        lambda r: _collect(check_retention_hold, r),
        lambda r: _collect(check_dependency_check, r),
        lambda r: _collect(check_archive_restore_test, r),
        lambda r: _collect(check_capacity_quota, r),
        lambda r: _collect(check_purge_audit, r),
        lambda r: _collect(check_lifecycle_manager_module, r),
        lambda r: _collect(check_audit_hash_chain, r),
        lambda r: _collect(check_reconcile_batch_digest, r),
        lambda r: _collect(check_resource_content_hash, r),
        lambda r: _collect(check_sqlite_database_digest, r),
        lambda r: _collect(check_qdrant_integrity_mapping, r),
        lambda r: _collect(check_merkle_root, r),
        lambda r: _collect(check_integrity_snapshot, r),
        lambda r: _collect(check_restore_verification, r),
        lambda r: _collect(check_tamper_state, r),
        lambda r: _collect(check_fail_closed, r),
        lambda r: _collect(check_integrity_verifier_module, r),
        lambda r: _collect(check_version_lock, r),
        lambda r: _collect(check_compatibility_matrix_ext, r),
        lambda r: _collect(check_upgrade_classification, r),
        lambda r: _collect(check_driver_compatibility_test, r),
        lambda r: _collect(check_pg_major_upgrade_rehearsal, r),
        lambda r: _collect(check_sqlite_runtime_compat, r),
        lambda r: _collect(check_qdrant_contract_compat, r),
        lambda r: _collect(check_sbom_dependency_inventory, r),
        lambda r: _collect(check_vulnerability_risk, r),
        lambda r: _collect(check_dependency_drift, r),
        lambda r: _collect(check_offline_bundle, r),
        lambda r: _collect(check_release_signature, r),
        lambda r: _collect(check_dependency_governor_module, r),
        lambda r: _collect(check_recovery_plan, r),
        lambda r: _collect(check_recovery_incident, r),
        lambda r: _collect(check_recovery_state_machine, r),
        lambda r: _collect(check_pg_offline_recovery, r),
        lambda r: _collect(check_pg_recovery_verification, r),
        lambda r: _collect(check_reconcile_recovery_phase, r),
        lambda r: _collect(check_recovery_generation, r),
        lambda r: _collect(check_recovery_barrier, r),
        lambda r: _collect(check_transport_recovery, r),
        lambda r: _collect(check_lease_recovery, r),
        lambda r: _collect(check_sqlite_fallback_freeze, r),
        lambda r: _collect(check_qdrant_recovery, r),
        lambda r: _collect(check_qdrant_full_rebuild, r),
        lambda r: _collect(check_recovery_checkpoint, r),
        lambda r: _collect(check_recovery_idempotency, r),
        lambda r: _collect(check_recovery_safety_fence, r),
        lambda r: _collect(check_chaos_drill, r),
        lambda r: _collect(check_recovery_certification, r),
        lambda r: _collect(check_recovery_orchestrator_module, r),
        lambda r: _collect(check_data_layer_contract, r),
        lambda r: _collect(check_dependency_classification, r),
        lambda r: _collect(check_startup_phase, r),
        lambda r: _collect(check_startup_phase_gate, r),
        lambda r: _collect(check_schema_readiness, r),
        lambda r: _collect(check_rag_readiness_gate, r),
        lambda r: _collect(check_shutdown_phase, r),
        lambda r: _collect(check_shutdown_audit, r),
        lambda r: _collect(check_unclean_shutdown_detection, r),
        lambda r: _collect(check_cache_invalidation_policy, r),
        lambda r: _collect(check_dependency_graph, r),
        lambda r: _collect(check_integration_rule, r),
        lambda r: _collect(check_data_layer_contract_module, r),
        lambda r: _collect(check_embedded_browser, r),
    ]
    if include_self_health:
        checks.append(
            lambda r: _collect(_verify_self_health_test_files, r)
        )

    # Executor.map preserves submission order; a check raising is contained
    # as an error entry rather than aborting the remaining checks.
    with ThreadPoolExecutor(
        max_workers=_audit_workers(len(checks)),
        thread_name_prefix="governance-audit",
    ) as executor:
        results = list(executor.map(lambda check: check(root), checks))
    for result in results:
        errors.extend(result)

    budget_error = audit_flow_budget_error(time.monotonic() - started)
    if budget_error:
        errors.append(budget_error)

    return errors
