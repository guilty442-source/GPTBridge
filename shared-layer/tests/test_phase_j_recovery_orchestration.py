"""Tests for Phase J: Recovery Orchestration.

Tests migrations 088-112, runtime helpers, and query allowlist.
"""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.database.query_allowlist import is_allowlisted
from shared_layer.database.schema_contract_registry import (
    declared_contract,
    EXPECTED_MIGRATION_COUNT,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _PROJECT_ROOT / "shared-layer" / "migrations"

class TestRecoveryPlanMigration:
    def test_088_exists(self):
        assert (_MIGRATIONS_DIR / "088_recovery_plan.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "088_recovery_plan.sql").read_text("utf-8")
        assert "recovery_plan" in text
        assert "incident_type" in text
        assert "steps" in text
        assert "verification_rules" in text
        assert "required_authority" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "088_recovery_plan.sql").read_text("utf-8")
        assert "register_recovery_plan" in text
        assert "certify_recovery_plan" in text
        assert "activate_recovery_plan" in text
        assert "get_active_recovery_plan" in text

class TestRecoveryIncidentMigration:
    def test_089_exists(self):
        assert (_MIGRATIONS_DIR / "089_recovery_incident.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "089_recovery_incident.sql").read_text("utf-8")
        assert "recovery_incident" in text
        assert "incident_type" in text
        assert "severity" in text
        assert "status" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "089_recovery_incident.sql").read_text("utf-8")
        assert "open_recovery_incident" in text
        assert "advance_incident_status" in text
        assert "get_active_incidents" in text

class TestRecoveryStateMachineMigration:
    def test_090_exists(self):
        assert (_MIGRATIONS_DIR / "090_recovery_state_machine.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "090_recovery_state_machine.sql").read_text("utf-8")
        assert "recovery_state_machine" in text
        assert "recovery_state_transition" in text
        assert "HEALTHY" in text
        assert "DEGRADED" in text
        assert "RECOVERING" in text
        assert "QUARANTINED" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "090_recovery_state_machine.sql").read_text("utf-8")
        assert "transition_recovery_state" in text
        assert "get_current_recovery_state" in text

class TestPgOfflineRecoveryMigration:
    def test_091_exists(self):
        assert (_MIGRATIONS_DIR / "091_pg_offline_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "091_pg_offline_recovery.sql").read_text("utf-8")
        assert "pg_offline_recovery" in text
        assert "fallback_status" in text
        assert "confirmation_attempts" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "091_pg_offline_recovery.sql").read_text("utf-8")
        assert "confirm_pg_failure" in text
        assert "enter_degraded_mode" in text

class TestPgRecoveryVerificationMigration:
    def test_092_exists(self):
        assert (_MIGRATIONS_DIR / "092_pg_recovery_verification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "092_pg_recovery_verification.sql").read_text("utf-8")
        assert "pg_recovery_verification" in text
        assert "connection_ok" in text
        assert "schema_version_ok" in text
        assert "rls_ok" in text
        assert "audit_ok" in text
        assert "transport_ok" in text
        assert "integrity_ok" in text
        assert "generation_ok" in text
        assert "overall_recoverable" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "092_pg_recovery_verification.sql").read_text("utf-8")
        assert "record_pg_recovery_verification" in text
        assert "is_pg_recoverable" in text

class TestReconcileRecoveryPhaseMigration:
    def test_093_exists(self):
        assert (_MIGRATIONS_DIR / "093_reconcile_recovery_phase.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "093_reconcile_recovery_phase.sql").read_text("utf-8")
        assert "reconcile_recovery_phase" in text
        assert "pending_snapshot_count" in text
        assert "reconciled_count" in text
        assert "conflict_count" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "093_reconcile_recovery_phase.sql").read_text("utf-8")
        assert "start_reconcile_recovery" in text
        assert "advance_reconcile_recovery" in text
        assert "is_reconcile_complete" in text

class TestRecoveryGenerationMigration:
    def test_094_exists(self):
        assert (_MIGRATIONS_DIR / "094_recovery_generation.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "094_recovery_generation.sql").read_text("utf-8")
        assert "recovery_generation" in text
        assert "generation_number" in text
        assert "generation_type" in text
        assert "degraded" in text
        assert "recovered" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "094_recovery_generation.sql").read_text("utf-8")
        assert "create_recovery_generation" in text
        assert "get_current_generation" in text

class TestRecoveryBarrierMigration:
    def test_095_exists(self):
        assert (_MIGRATIONS_DIR / "095_recovery_barrier.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "095_recovery_barrier.sql").read_text("utf-8")
        assert "recovery_barrier" in text
        assert "RECOVERING_READ_ONLY" in text
        assert "barrier_active" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "095_recovery_barrier.sql").read_text("utf-8")
        assert "raise_recovery_barrier" in text
        assert "release_recovery_barrier" in text
        assert "is_recovery_barrier_active" in text

class TestTransportRecoveryMigration:
    def test_096_exists(self):
        assert (_MIGRATIONS_DIR / "096_transport_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "096_transport_recovery.sql").read_text("utf-8")
        assert "transport_recovery" in text
        assert "idempotency_key" in text
        assert "COMMITTED" in text
        assert "NOT_COMMITTED" in text
        assert "UNKNOWN" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "096_transport_recovery.sql").read_text("utf-8")
        assert "register_unknown_commit" in text
        assert "resolve_commit_state" in text
        assert "get_unknown_commits" in text

class TestUnknownCommitResolutionMigration:
    def test_097_exists(self):
        assert (_MIGRATIONS_DIR / "097_unknown_commit_resolution.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "097_unknown_commit_resolution.sql").read_text("utf-8")
        assert "unknown_commit_resolution" in text
        assert "lookup_method" in text
        assert "found_committed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "097_unknown_commit_resolution.sql").read_text("utf-8")
        assert "record_commit_lookup" in text

class TestLeaseRecoveryMigration:
    def test_098_exists(self):
        assert (_MIGRATIONS_DIR / "098_lease_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "098_lease_recovery.sql").read_text("utf-8")
        assert "lease_recovery" in text
        assert "lease_until" in text
        assert "worker_generation" in text
        assert "lease_status" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "098_lease_recovery.sql").read_text("utf-8")
        assert "check_lease_expiry" in text
        assert "reclaim_lease" in text
        assert "get_expired_leases" in text

class TestSqliteFallbackFreezeMigration:
    def test_099_exists(self):
        assert (_MIGRATIONS_DIR / "099_sqlite_fallback_freeze.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "099_sqlite_fallback_freeze.sql").read_text("utf-8")
        assert "sqlite_fallback_freeze" in text
        assert "fallback_open" in text
        assert "fallback_draining" in text
        assert "fallback_frozen" in text
        assert "fallback_closed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "099_sqlite_fallback_freeze.sql").read_text("utf-8")
        assert "transition_fallback_state" in text
        assert "get_fallback_state" in text

class TestRecoveryPriorityMigration:
    def test_100_exists(self):
        assert (_MIGRATIONS_DIR / "100_recovery_priority.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "100_recovery_priority.sql").read_text("utf-8")
        assert "recovery_priority" in text
        assert "P0_authority_security" in text
        assert "P1_transport_critical" in text
        assert "P2_active_resource" in text
        assert "P3_rag_metadata" in text
        assert "P4_historical_analytics" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "100_recovery_priority.sql").read_text("utf-8")
        assert "get_recovery_priority" in text

class TestQdrantRecoveryMigration:
    def test_101_exists(self):
        assert (_MIGRATIONS_DIR / "101_qdrant_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "101_qdrant_recovery.sql").read_text("utf-8")
        assert "qdrant_recovery" in text
        assert "qdrant_status" in text
        assert "indexing_backlog_count" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "101_qdrant_recovery.sql").read_text("utf-8")
        assert "start_qdrant_recovery" in text
        assert "update_qdrant_recovery" in text

class TestQdrantFullRebuildMigration:
    def test_102_exists(self):
        assert (_MIGRATIONS_DIR / "102_qdrant_full_rebuild.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "102_qdrant_full_rebuild.sql").read_text("utf-8")
        assert "qdrant_full_rebuild" in text
        assert "collection_generation" in text
        assert "old_collection_retired" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "102_qdrant_full_rebuild.sql").read_text("utf-8")
        assert "start_qdrant_full_rebuild" in text
        assert "advance_qdrant_rebuild" in text

class TestSqliteSingleRecoveryMigration:
    def test_103_exists(self):
        assert (_MIGRATIONS_DIR / "103_sqlite_single_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "103_sqlite_single_recovery.sql").read_text("utf-8")
        assert "sqlite_single_recovery" in text
        assert "failure_type" in text
        assert "recovery_action" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "103_sqlite_single_recovery.sql").read_text("utf-8")
        assert "start_sqlite_recovery" in text
        assert "complete_sqlite_recovery" in text

class TestCodexSqliteRecoveryMigration:
    def test_104_exists(self):
        assert (_MIGRATIONS_DIR / "104_codex_sqlite_recovery.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "104_codex_sqlite_recovery.sql").read_text("utf-8")
        assert "codex_sqlite_recovery" in text
        assert "quarantined" in text
        assert "hash_verified" in text
        assert "restored_from_source" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "104_codex_sqlite_recovery.sql").read_text("utf-8")
        assert "start_codex_recovery" in text
        assert "advance_codex_recovery" in text

class TestBackupRestoreOrchestrationMigration:
    def test_105_exists(self):
        assert (_MIGRATIONS_DIR / "105_backup_restore_orchestration.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "105_backup_restore_orchestration.sql").read_text("utf-8")
        assert "backup_restore_orchestration" in text
        assert "backup_id" in text
        assert "hash_verified" in text
        assert "schema_verified" in text
        assert "rls_verified" in text
        assert "audit_verified" in text
        assert "integrity_verified" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "105_backup_restore_orchestration.sql").read_text("utf-8")
        assert "start_backup_restore" in text
        assert "advance_backup_restore" in text

class TestPitrBoundaryMigration:
    def test_106_exists(self):
        assert (_MIGRATIONS_DIR / "106_pitr_boundary.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "106_pitr_boundary.sql").read_text("utf-8")
        assert "pitr_boundary" in text
        assert "restore_target" in text
        assert "database_generation" in text
        assert "audit_head_hash" in text
        assert "transport_cutoff" in text
        assert "reconcile_cutoff" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "106_pitr_boundary.sql").read_text("utf-8")
        assert "record_pitr_boundary" in text
        assert "complete_pitr_reconcile" in text

class TestRecoveryRetryPolicyMigration:
    def test_107_exists(self):
        assert (_MIGRATIONS_DIR / "107_recovery_retry_policy.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "107_recovery_retry_policy.sql").read_text("utf-8")
        assert "recovery_retry_policy" in text
        assert "max_attempts" in text
        assert "timeout_seconds" in text
        assert "backoff_strategy" in text
        assert "escalation_action" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "107_recovery_retry_policy.sql").read_text("utf-8")
        assert "register_retry_policy" in text
        assert "get_retry_policy" in text

class TestRecoveryCheckpointMigration:
    def test_108_exists(self):
        assert (_MIGRATIONS_DIR / "108_recovery_checkpoint.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "108_recovery_checkpoint.sql").read_text("utf-8")
        assert "recovery_checkpoint" in text
        assert "current_phase" in text
        assert "last_processed_revision" in text
        assert "batch_cursor" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "108_recovery_checkpoint.sql").read_text("utf-8")
        assert "save_recovery_checkpoint" in text
        assert "resume_recovery_checkpoint" in text
        assert "get_latest_checkpoint" in text

class TestRecoveryIdempotencyMigration:
    def test_109_exists(self):
        assert (_MIGRATIONS_DIR / "109_recovery_idempotency.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "109_recovery_idempotency.sql").read_text("utf-8")
        assert "recovery_idempotency" in text
        assert "operation_key" in text
        assert "operation_type" in text
        assert "completed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "109_recovery_idempotency.sql").read_text("utf-8")
        assert "check_or_mark_idempotent" in text
        assert "mark_idempotent_complete" in text

class TestRecoverySafetyFenceMigration:
    def test_110_exists(self):
        assert (_MIGRATIONS_DIR / "110_recovery_safety_fence.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "110_recovery_safety_fence.sql").read_text("utf-8")
        assert "recovery_safety_fence" in text
        assert "forbidden_action" in text
        assert "drop_authoritative_database" in text
        assert "truncate_official_data" in text
        assert "rewrite_governance_codex" in text
        assert "change_rls_policy" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "110_recovery_safety_fence.sql").read_text("utf-8")
        assert "check_safety_fence" in text
        assert "record_fence_block" in text

class TestChaosDrillMigration:
    def test_111_exists(self):
        assert (_MIGRATIONS_DIR / "111_chaos_drill.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "111_chaos_drill.sql").read_text("utf-8")
        assert "chaos_drill" in text
        assert "scenario_code" in text
        assert "no_authority_inversion" in text
        assert "no_duplicate_write" in text
        assert "no_lost_commit" in text
        assert "no_silent_conflict" in text
        assert "no_uncontrolled_retry" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "111_chaos_drill.sql").read_text("utf-8")
        assert "start_chaos_drill" in text
        assert "complete_chaos_drill" in text

class TestRecoveryCertificationMigration:
    def test_112_exists(self):
        assert (_MIGRATIONS_DIR / "112_recovery_certification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "112_recovery_certification.sql").read_text("utf-8")
        assert "recovery_certification" in text
        assert "certification_status" in text
        assert "integrity_result" in text
        assert "reconcile_result" in text
        assert "audit_result" in text
        assert "CERTIFIED" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "112_recovery_certification.sql").read_text("utf-8")
        assert "record_recovery_certification" in text
        assert "certify_recovery_plan_v2" in text
        assert "is_recovery_plan_certified" in text

class TestRecoveryOrchestratorModule:
    def test_import_register_recovery_plan(self):
        from shared_layer.database.recovery_orchestrator import register_recovery_plan
        assert callable(register_recovery_plan)

    def test_import_open_recovery_incident(self):
        from shared_layer.database.recovery_orchestrator import open_recovery_incident
        assert callable(open_recovery_incident)

    def test_import_transition_recovery_state(self):
        from shared_layer.database.recovery_orchestrator import transition_recovery_state
        assert callable(transition_recovery_state)

    def test_import_create_recovery_generation(self):
        from shared_layer.database.recovery_orchestrator import create_recovery_generation
        assert callable(create_recovery_generation)

    def test_import_get_current_generation(self):
        from shared_layer.database.recovery_orchestrator import get_current_generation
        assert callable(get_current_generation)

    def test_import_start_pg_offline_recovery(self):
        from shared_layer.database.recovery_orchestrator import start_pg_offline_recovery
        assert callable(start_pg_offline_recovery)

    def test_import_confirm_pg_failure(self):
        from shared_layer.database.recovery_orchestrator import confirm_pg_failure
        assert callable(confirm_pg_failure)

    def test_import_record_pg_recovery_verification(self):
        from shared_layer.database.recovery_orchestrator import record_pg_recovery_verification
        assert callable(record_pg_recovery_verification)

    def test_import_is_pg_recoverable(self):
        from shared_layer.database.recovery_orchestrator import is_pg_recoverable
        assert callable(is_pg_recoverable)

    def test_import_register_unknown_commit(self):
        from shared_layer.database.recovery_orchestrator import register_unknown_commit
        assert callable(register_unknown_commit)

    def test_import_resolve_commit_state(self):
        from shared_layer.database.recovery_orchestrator import resolve_commit_state
        assert callable(resolve_commit_state)

    def test_import_check_lease_expiry(self):
        from shared_layer.database.recovery_orchestrator import check_lease_expiry
        assert callable(check_lease_expiry)

    def test_import_reclaim_lease(self):
        from shared_layer.database.recovery_orchestrator import reclaim_lease
        assert callable(reclaim_lease)

    def test_import_raise_recovery_barrier(self):
        from shared_layer.database.recovery_orchestrator import raise_recovery_barrier
        assert callable(raise_recovery_barrier)

    def test_import_is_recovery_barrier_active(self):
        from shared_layer.database.recovery_orchestrator import is_recovery_barrier_active
        assert callable(is_recovery_barrier_active)

    def test_import_release_recovery_barrier(self):
        from shared_layer.database.recovery_orchestrator import release_recovery_barrier
        assert callable(release_recovery_barrier)

    def test_import_save_recovery_checkpoint(self):
        from shared_layer.database.recovery_orchestrator import save_recovery_checkpoint
        assert callable(save_recovery_checkpoint)

    def test_import_check_or_mark_idempotent(self):
        from shared_layer.database.recovery_orchestrator import check_or_mark_idempotent
        assert callable(check_or_mark_idempotent)

    def test_import_mark_idempotent_complete(self):
        from shared_layer.database.recovery_orchestrator import mark_idempotent_complete
        assert callable(mark_idempotent_complete)

    def test_import_check_safety_fence(self):
        from shared_layer.database.recovery_orchestrator import check_safety_fence
        assert callable(check_safety_fence)

    def test_import_record_fence_block(self):
        from shared_layer.database.recovery_orchestrator import record_fence_block
        assert callable(record_fence_block)

    def test_import_is_recovery_plan_certified(self):
        from shared_layer.database.recovery_orchestrator import is_recovery_plan_certified
        assert callable(is_recovery_plan_certified)

    def test_import_start_chaos_drill(self):
        from shared_layer.database.recovery_orchestrator import start_chaos_drill
        assert callable(start_chaos_drill)

    def test_import_complete_chaos_drill(self):
        from shared_layer.database.recovery_orchestrator import complete_chaos_drill
        assert callable(complete_chaos_drill)

    def test_lazy_export_via_init(self):
        from shared_layer.database import open_recovery_incident, create_recovery_generation
        assert callable(open_recovery_incident)
        assert callable(create_recovery_generation)

class TestQueryAllowlistPhaseJ:
    def test_recovery_plan_query(self):
        assert is_allowlisted("recovery_plan.active")

    def test_recovery_incident_query(self):
        assert is_allowlisted("recovery_incident.active")

    def test_recovery_state_query(self):
        assert is_allowlisted("recovery_state.current")

    def test_pg_offline_query(self):
        assert is_allowlisted("pg_offline_recovery.list")

    def test_pg_recovery_verify_query(self):
        assert is_allowlisted("pg_recovery_verify.list")

    def test_reconcile_recovery_query(self):
        assert is_allowlisted("reconcile_recovery.list")

    def test_recovery_generation_query(self):
        assert is_allowlisted("recovery_generation.current")

    def test_recovery_barrier_query(self):
        assert is_allowlisted("recovery_barrier.active")

    def test_transport_recovery_query(self):
        assert is_allowlisted("transport_recovery.unknown")

    def test_lease_recovery_query(self):
        assert is_allowlisted("lease_recovery.expired")

    def test_sqlite_fallback_query(self):
        assert is_allowlisted("sqlite_fallback.state")

    def test_qdrant_recovery_query(self):
        assert is_allowlisted("qdrant_recovery.list")

    def test_qdrant_full_rebuild_query(self):
        assert is_allowlisted("qdrant_full_rebuild.list")

    def test_chaos_drill_query(self):
        assert is_allowlisted("chaos_drill.list")

    def test_recovery_certification_query(self):
        assert is_allowlisted("recovery_certification.list")

class TestSchemaContractRegistryPhaseJ:
    def test_expected_migration_count_is_125(self):
        import pathlib

        migrations = pathlib.Path(__file__).resolve().parents[1] / "migrations"
        assert EXPECTED_MIGRATION_COUNT == len(list(migrations.glob("*.sql")))

    def test_contract_includes_recovery_plan(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_plan") in table_names

    def test_contract_includes_recovery_incident(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_incident") in table_names

    def test_contract_includes_recovery_state_machine(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_state_machine") in table_names

    def test_contract_includes_recovery_state_transition(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_state_transition") in table_names

    def test_contract_includes_pg_offline_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "pg_offline_recovery") in table_names

    def test_contract_includes_pg_recovery_verification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "pg_recovery_verification") in table_names

    def test_contract_includes_reconcile_recovery_phase(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "reconcile_recovery_phase") in table_names

    def test_contract_includes_recovery_generation(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_generation") in table_names

    def test_contract_includes_recovery_barrier(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_barrier") in table_names

    def test_contract_includes_transport_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "transport_recovery") in table_names

    def test_contract_includes_unknown_commit_resolution(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "unknown_commit_resolution") in table_names

    def test_contract_includes_lease_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "lease_recovery") in table_names

    def test_contract_includes_sqlite_fallback_freeze(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_fallback_freeze") in table_names

    def test_contract_includes_recovery_priority(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_priority") in table_names

    def test_contract_includes_qdrant_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_recovery") in table_names

    def test_contract_includes_qdrant_full_rebuild(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_full_rebuild") in table_names

    def test_contract_includes_sqlite_single_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_single_recovery") in table_names

    def test_contract_includes_codex_sqlite_recovery(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "codex_sqlite_recovery") in table_names

    def test_contract_includes_backup_restore_orchestration(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "backup_restore_orchestration") in table_names

    def test_contract_includes_pitr_boundary(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "pitr_boundary") in table_names

    def test_contract_includes_recovery_retry_policy(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_retry_policy") in table_names

    def test_contract_includes_recovery_checkpoint(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_checkpoint") in table_names

    def test_contract_includes_recovery_idempotency(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_idempotency") in table_names

    def test_contract_includes_recovery_safety_fence(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_safety_fence") in table_names

    def test_contract_includes_chaos_drill(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "chaos_drill") in table_names

    def test_contract_includes_recovery_certification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "recovery_certification") in table_names
