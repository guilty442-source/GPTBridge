"""Tests for Phase G: Data Lifecycle Management.

Tests migrations 050-057, 063-067, runtime helpers, and query allowlist.
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


class TestUnifiedLifecycleStateMigration:
    def test_050_exists(self):
        assert (_MIGRATIONS_DIR / "050_unified_lifecycle_state.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "050_unified_lifecycle_state.sql").read_text("utf-8")
        assert "lifecycle_state" in text
        assert "entity_type" in text
        assert "entity_id" in text

    def test_defines_all_states(self):
        text = (_MIGRATIONS_DIR / "050_unified_lifecycle_state.sql").read_text("utf-8")
        for state in ("ACTIVE", "STALE", "SUPERSEDED", "TOMBSTONED",
                      "ARCHIVED", "PURGED"):
            assert state in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "050_unified_lifecycle_state.sql").read_text("utf-8")
        assert "transition_lifecycle_state" in text
        assert "get_lifecycle_state" in text
        assert "get_entities_by_state" in text


class TestTransportRetentionMigration:
    def test_051_exists(self):
        assert (_MIGRATIONS_DIR / "051_transport_retention.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "051_transport_retention.sql").read_text("utf-8")
        assert "transport_retention_policy" in text
        assert "hot_retention_days" in text
        assert "archive_after_days" in text

    def test_defines_status_policies(self):
        text = (_MIGRATIONS_DIR / "051_transport_retention.sql").read_text("utf-8")
        for status in ("queued", "claimed", "completed", "failed", "dead_letter"):
            assert status in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "051_transport_retention.sql").read_text("utf-8")
        assert "get_transport_archive_eligible" in text
        assert "get_transport_purge_eligible" in text


class TestAuditRetentionLayeringMigration:
    def test_052_exists(self):
        assert (_MIGRATIONS_DIR / "052_audit_retention_layering.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "052_audit_retention_layering.sql").read_text("utf-8")
        assert "audit_retention_layer" in text
        assert "retention_days" in text
        assert "compression_enabled" in text

    def test_defines_layers(self):
        text = (_MIGRATIONS_DIR / "052_audit_retention_layering.sql").read_text("utf-8")
        for layer in ("hot", "archive", "long_term"):
            assert layer in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "052_audit_retention_layering.sql").read_text("utf-8")
        assert "get_audit_archive_eligible" in text
        assert "get_audit_long_term_eligible" in text


class TestSqlitePerClassRetentionMigration:
    def test_053_exists(self):
        assert (_MIGRATIONS_DIR / "053_sqlite_per_class_retention.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "053_sqlite_per_class_retention.sql").read_text("utf-8")
        assert "sqlite_retention_policy" in text
        assert "retention_days" in text
        assert "archive_eligible" in text
        assert "purge_eligible" in text
        assert "version_history_required" in text

    def test_defines_class_policies(self):
        text = (_MIGRATIONS_DIR / "053_sqlite_per_class_retention.sql").read_text("utf-8")
        for cls in ("'A'", "'B'", "'C'", "'D'"):
            assert cls in text

    def test_defines_function(self):
        text = (_MIGRATIONS_DIR / "053_sqlite_per_class_retention.sql").read_text("utf-8")
        assert "get_sqlite_retention_for_class" in text


class TestQdrantVectorLifecycleMigration:
    def test_054_exists(self):
        assert (_MIGRATIONS_DIR / "054_qdrant_vector_lifecycle.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "054_qdrant_vector_lifecycle.sql").read_text("utf-8")
        assert "qdrant_vector_lifecycle" in text
        assert "vector_state" in text
        assert "point_id" in text
        assert "pg_marked_at" in text
        assert "pg_confirmed_at" in text

    def test_defines_vector_states(self):
        text = (_MIGRATIONS_DIR / "054_qdrant_vector_lifecycle.sql").read_text("utf-8")
        for state in ("ACTIVE", "STALE", "RETRIEVAL_FORBIDDEN",
                      "DELETE_PENDING", "DELETED", "VERIFIED_DELETED"):
            assert state in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "054_qdrant_vector_lifecycle.sql").read_text("utf-8")
        assert "mark_vector_for_resource_state" in text
        assert "confirm_vector_deleted" in text
        assert "get_vectors_pending_deletion" in text


class TestPurgeQueueMigration:
    def test_055_exists(self):
        assert (_MIGRATIONS_DIR / "055_purge_queue.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "055_purge_queue.sql").read_text("utf-8")
        assert "purge_queue" in text
        assert "resource_id" in text
        assert "retention_until" in text
        assert "reason" in text
        assert "approval_id" in text
        assert "purge_status" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "055_purge_queue.sql").read_text("utf-8")
        assert "enqueue_purge" in text
        assert "approve_purge" in text
        assert "get_purge_eligible" in text
        assert "mark_purged" in text


class TestArchiveCatalogMigration:
    def test_056_exists(self):
        assert (_MIGRATIONS_DIR / "056_archive_catalog.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "056_archive_catalog.sql").read_text("utf-8")
        assert "archive_catalog" in text
        assert "source_engine" in text
        assert "storage_locator" in text
        assert "integrity_hash" in text
        assert "record_count" in text
        assert "schema_version" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "056_archive_catalog.sql").read_text("utf-8")
        assert "register_archive" in text
        assert "verify_archive" in text
        assert "find_archives" in text


class TestArchiveVersioningMigration:
    def test_057_exists(self):
        assert (_MIGRATIONS_DIR / "057_archive_versioning.sql").is_file()

    def test_defines_view(self):
        text = (_MIGRATIONS_DIR / "057_archive_versioning.sql").read_text("utf-8")
        assert "archive_version_manifest" in text

    def test_defines_versioning_columns(self):
        text = (_MIGRATIONS_DIR / "057_archive_versioning.sql").read_text("utf-8")
        assert "encoding" in text
        assert "archive_format_version" in text
        assert "checksum_algorithm" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "057_archive_versioning.sql").read_text("utf-8")
        assert "mark_restore_tested" in text
        assert "get_untested_archives" in text


class TestRetentionHoldMigration:
    def test_063_exists(self):
        assert (_MIGRATIONS_DIR / "059_retention_hold.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "059_retention_hold.sql").read_text("utf-8")
        assert "retention_hold" in text
        assert "hold_reason" in text
        assert "hold_active" in text

    def test_defines_hold_reasons(self):
        text = (_MIGRATIONS_DIR / "059_retention_hold.sql").read_text("utf-8")
        for reason in ("audit_investigation", "governance_review",
                       "legal_hold", "compliance_hold"):
            assert reason in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "059_retention_hold.sql").read_text("utf-8")
        assert "place_hold" in text
        assert "release_hold" in text
        assert "has_active_hold" in text


class TestDependencyCheckMigration:
    def test_064_exists(self):
        assert (_MIGRATIONS_DIR / "060_dependency_check.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "060_dependency_check.sql").read_text("utf-8")
        assert "dependency_check" in text
        assert "has_dependencies" in text
        assert "can_purge" in text
        assert "dependency_details" in text

    def test_defines_function(self):
        text = (_MIGRATIONS_DIR / "060_dependency_check.sql").read_text("utf-8")
        assert "check_resource_dependencies" in text


class TestArchiveRestoreTestMigration:
    def test_065_exists(self):
        assert (_MIGRATIONS_DIR / "061_archive_restore_test.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "061_archive_restore_test.sql").read_text("utf-8")
        assert "archive_restore_test" in text
        assert "schema_check_passed" in text
        assert "row_count_match" in text
        assert "hash_verify_passed" in text
        assert "query_test_passed" in text
        assert "overall_passed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "061_archive_restore_test.sql").read_text("utf-8")
        assert "record_restore_test" in text
        assert "get_failed_restore_tests" in text


class TestCapacityQuotaMigration:
    def test_066_exists(self):
        assert (_MIGRATIONS_DIR / "062_capacity_quota.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "062_capacity_quota.sql").read_text("utf-8")
        assert "capacity_quota" in text
        assert "soft_limit_mb" in text
        assert "hard_limit_mb" in text
        assert "archive_threshold_mb" in text
        assert "emergency_threshold_mb" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "062_capacity_quota.sql").read_text("utf-8")
        assert "update_capacity_measurement" in text
        assert "check_capacity_status" in text


class TestPurgeAuditMigration:
    def test_067_exists(self):
        assert (_MIGRATIONS_DIR / "063_purge_audit.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "063_purge_audit.sql").read_text("utf-8")
        assert "purge_audit_log" in text
        assert "previous_hash" in text
        assert "deleted_from" in text
        assert "audit_hash" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "063_purge_audit.sql").read_text("utf-8")
        assert "record_purge" in text
        assert "verify_purge" in text
        assert "get_purge_history" in text


class TestLifecycleManagerModule:
    def test_import_transition_state(self):
        from shared_layer.database.lifecycle_manager import transition_state
        assert callable(transition_state)

    def test_import_get_state(self):
        from shared_layer.database.lifecycle_manager import get_state
        assert callable(get_state)

    def test_import_enqueue_purge(self):
        from shared_layer.database.lifecycle_manager import enqueue_purge
        assert callable(enqueue_purge)

    def test_import_get_purge_eligible(self):
        from shared_layer.database.lifecycle_manager import get_purge_eligible
        assert callable(get_purge_eligible)

    def test_import_check_dependencies(self):
        from shared_layer.database.lifecycle_manager import check_dependencies
        assert callable(check_dependencies)

    def test_import_record_purge(self):
        from shared_layer.database.lifecycle_manager import record_purge
        assert callable(record_purge)

    def test_import_place_hold(self):
        from shared_layer.database.lifecycle_manager import place_hold
        assert callable(place_hold)

    def test_import_release_hold(self):
        from shared_layer.database.lifecycle_manager import release_hold
        assert callable(release_hold)

    def test_import_has_active_hold(self):
        from shared_layer.database.lifecycle_manager import has_active_hold
        assert callable(has_active_hold)

    def test_import_register_archive(self):
        from shared_layer.database.lifecycle_manager import register_archive
        assert callable(register_archive)

    def test_lazy_export_via_init(self):
        from shared_layer.database import transition_lifecycle_state, get_lifecycle_state
        assert callable(transition_lifecycle_state)
        assert callable(get_lifecycle_state)


class TestQueryAllowlistPhaseG:
    def test_lifecycle_state_queries(self):
        assert is_allowlisted("lifecycle_state.get")
        assert is_allowlisted("lifecycle_state.by_state")

    def test_transport_retention_queries(self):
        assert is_allowlisted("transport_retention.list")
        assert is_allowlisted("transport_retention.archive_eligible")

    def test_audit_retention_query(self):
        assert is_allowlisted("audit_retention.layers")

    def test_sqlite_retention_query(self):
        assert is_allowlisted("sqlite_retention.by_class")

    def test_qdrant_vector_query(self):
        assert is_allowlisted("qdrant_vector.pending_deletion")

    def test_purge_queue_queries(self):
        assert is_allowlisted("purge_queue.eligible")
        assert is_allowlisted("purge_queue.list")

    def test_archive_catalog_query(self):
        assert is_allowlisted("archive_catalog.list")

    def test_retention_hold_query(self):
        assert is_allowlisted("retention_hold.active")

    def test_dependency_check_query(self):
        assert is_allowlisted("dependency_check.recent")

    def test_archive_restore_test_query(self):
        assert is_allowlisted("archive_restore_test.recent")

    def test_capacity_quota_query(self):
        assert is_allowlisted("capacity_quota.list")

    def test_purge_audit_query(self):
        assert is_allowlisted("purge_audit.recent")


class TestSchemaContractRegistryPhaseG:
    def test_expected_migration_count_is_73(self):
        assert EXPECTED_MIGRATION_COUNT == 73

    def test_contract_includes_lifecycle_state(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "lifecycle_state") in table_names

    def test_contract_includes_transport_retention_policy(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "transport_retention_policy") in table_names

    def test_contract_includes_audit_retention_layer(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "audit_retention_layer") in table_names

    def test_contract_includes_sqlite_retention_policy(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_retention_policy") in table_names

    def test_contract_includes_qdrant_vector_lifecycle(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_vector_lifecycle") in table_names

    def test_contract_includes_purge_queue(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "purge_queue") in table_names

    def test_contract_includes_archive_catalog(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "archive_catalog") in table_names

    def test_contract_includes_retention_hold(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "retention_hold") in table_names

    def test_contract_includes_dependency_check(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "dependency_check") in table_names

    def test_contract_includes_archive_restore_test(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "archive_restore_test") in table_names

    def test_contract_includes_capacity_quota(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "capacity_quota") in table_names

    def test_contract_includes_purge_audit_log(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "purge_audit_log") in table_names
