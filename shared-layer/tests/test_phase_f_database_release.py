"""Tests for Phase F: Database Release Management.

Tests migrations 040-049, runtime helpers, manifest file, and query allowlist.
"""
from __future__ import annotations

import json
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
_MANIFEST_PATH = _PROJECT_ROOT / "shared-layer" / "database-release.json"


class TestDatabaseReleaseManifestMigration:
    def test_040_exists(self):
        assert (_MIGRATIONS_DIR / "040_database_release_manifest.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "040_database_release_manifest.sql").read_text("utf-8")
        assert "database_release" in text
        assert "release_id" in text
        assert "schema_version" in text
        assert "migration_head" in text
        assert "rls_version" in text
        assert "role_version" in text
        assert "sqlite_template_version" in text
        assert "qdrant_contract_version" in text
        assert "query_contract_version" in text
        assert "minimum_runtime_version" in text
        assert "compatibility_range" in text

    def test_defines_state_machine(self):
        text = (_MIGRATIONS_DIR / "040_database_release_manifest.sql").read_text("utf-8")
        for state in ("DRAFT", "VALIDATED", "CERTIFIED", "STAGED",
                      "ACTIVE", "SUPERSEDED", "ARCHIVED", "REJECTED"):
            assert state in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "040_database_release_manifest.sql").read_text("utf-8")
        assert "transition_release_state" in text
        assert "get_active_release" in text
        assert "supersede_active_release" in text


class TestReleaseManifestFile:
    def test_manifest_exists(self):
        assert _MANIFEST_PATH.is_file()

    def test_manifest_valid_json(self):
        data = json.loads(_MANIFEST_PATH.read_text("utf-8"))
        assert isinstance(data, dict)

    def test_manifest_has_required_keys(self):
        data = json.loads(_MANIFEST_PATH.read_text("utf-8"))
        for key in ("release_id", "schema_version", "migration_head",
                    "rls_version", "role_version", "sqlite_template_version",
                    "reconcile_contract_version", "qdrant_contract_version",
                    "query_contract_version", "minimum_runtime_version",
                    "compatibility_range", "state"):
            assert key in data, f"manifest missing key: {key}"

    def test_manifest_has_compatibility_range(self):
        data = json.loads(_MANIFEST_PATH.read_text("utf-8"))
        compat = data["compatibility_range"]
        assert "min_runtime" in compat
        assert "max_runtime" in compat


class TestReleaseManifestModule:
    def test_import_load_manifest(self):
        from shared_layer.database.release_manifest import load_manifest
        assert callable(load_manifest)

    def test_import_validate_runtime(self):
        from shared_layer.database.release_manifest import validate_runtime
        assert callable(validate_runtime)

    def test_load_manifest_returns_dict(self):
        from shared_layer.database.release_manifest import load_manifest
        manifest = load_manifest()
        assert isinstance(manifest, dict)
        assert "release_id" in manifest

    def test_validate_runtime_full(self):
        from shared_layer.database.release_manifest import load_manifest, validate_runtime
        manifest = load_manifest()
        result = validate_runtime(manifest, runtime_version="1.0.0")
        assert result["compatible"] is True
        assert result["mode"] in ("full", "read-only")

    def test_validate_runtime_rejected(self):
        from shared_layer.database.release_manifest import load_manifest, validate_runtime
        manifest = load_manifest()
        result = validate_runtime(manifest, runtime_version="0.0.1")
        # Very old runtime should be rejected or read-only
        assert result["mode"] in ("rejected", "read-only")

    def test_lazy_export_via_init(self):
        from shared_layer.database import load_release_manifest, validate_runtime_compatibility
        assert callable(load_release_manifest)
        assert callable(validate_runtime_compatibility)


class TestCompatibilityMatrixMigration:
    def test_041_exists(self):
        assert (_MIGRATIONS_DIR / "041_compatibility_matrix.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "041_compatibility_matrix.sql").read_text("utf-8")
        assert "release_compatibility" in text
        assert "mode" in text
        assert "full" in text
        assert "read-only" in text
        assert "rejected" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "041_compatibility_matrix.sql").read_text("utf-8")
        assert "check_compatibility" in text
        assert "upsert_compatibility" in text


class TestMigrationBreakingChangeMigration:
    def test_042_exists(self):
        assert (_MIGRATIONS_DIR / "042_migration_breaking_change.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "042_migration_breaking_change.sql").read_text("utf-8")
        assert "migration_classification" in text
        assert "compatible" in text
        assert "conditional" in text
        assert "breaking" in text

    def test_defines_breaking_requirements(self):
        text = (_MIGRATIONS_DIR / "042_migration_breaking_change.sql").read_text("utf-8")
        assert "pre_migration" in text
        assert "rollback_plan" in text
        assert "recovery_plan" in text
        assert "compatibility_window_days" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "042_migration_breaking_change.sql").read_text("utf-8")
        assert "classify_migration" in text
        assert "get_breaking_migrations" in text


class TestQueryContractVersionMigration:
    def test_043_exists(self):
        assert (_MIGRATIONS_DIR / "043_query_contract_version.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "043_query_contract_version.sql").read_text("utf-8")
        assert "query_contract" in text
        assert "contract_name" in text
        assert "version" in text
        assert "sql_template" in text
        assert "active" in text
        assert "deprecated" in text
        assert "retired" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "043_query_contract_version.sql").read_text("utf-8")
        assert "register_query_contract" in text
        assert "deprecate_query_contract" in text
        assert "retire_query_contract" in text
        assert "get_active_query_contract" in text


class TestRlsRoleMigrationMigration:
    def test_044_exists(self):
        assert (_MIGRATIONS_DIR / "044_rls_role_migration.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "044_rls_role_migration.sql").read_text("utf-8")
        assert "rls_role_migration" in text
        assert "rls_role_version" in text
        assert "change_type" in text
        assert "target_object" in text
        assert "change_sql" in text
        assert "rollback_sql" in text

    def test_defines_change_types(self):
        text = (_MIGRATIONS_DIR / "044_rls_role_migration.sql").read_text("utf-8")
        assert "create_role" in text
        assert "grant" in text
        assert "create_policy" in text
        assert "security_definer_grant" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "044_rls_role_migration.sql").read_text("utf-8")
        assert "record_rls_role_migration" in text
        assert "get_rls_role_version" in text


class TestSqliteTemplateReleaseMigration:
    def test_045_exists(self):
        assert (_MIGRATIONS_DIR / "045_sqlite_template_release.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "045_sqlite_template_release.sql").read_text("utf-8")
        assert "sqlite_template_release" in text
        assert "template_version" in text
        assert "schema_version" in text
        assert "minimum_reader_version" in text
        assert "minimum_writer_version" in text
        assert "ddl_hash" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "045_sqlite_template_release.sql").read_text("utf-8")
        assert "register_sqlite_template" in text
        assert "get_active_sqlite_template" in text
        assert "can_write_sqlite" in text


class TestQdrantContractVersionMigration:
    def test_046_exists(self):
        assert (_MIGRATIONS_DIR / "046_qdrant_contract_version.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "046_qdrant_contract_version.sql").read_text("utf-8")
        assert "qdrant_contract" in text
        assert "collection_name" in text
        assert "vector_dimension" in text
        assert "distance_metric" in text
        assert "embedding_model" in text
        assert "payload_schema" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "046_qdrant_contract_version.sql").read_text("utf-8")
        assert "register_qdrant_contract" in text
        assert "deprecate_qdrant_contract" in text
        assert "retire_qdrant_contract" in text
        assert "get_active_qdrant_contract" in text


class TestCanaryUpgradeMigration:
    def test_047_exists(self):
        assert (_MIGRATIONS_DIR / "047_canary_upgrade.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "047_canary_upgrade.sql").read_text("utf-8")
        assert "canary_upgrade" in text
        assert "release_id" in text
        assert "canary_database_name" in text
        assert "certification_id" in text
        assert "promoted_to_production" in text

    def test_defines_states(self):
        text = (_MIGRATIONS_DIR / "047_canary_upgrade.sql").read_text("utf-8")
        for state in ("restoring", "migrating", "certifying",
                      "succeeded", "failed"):
            assert state in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "047_canary_upgrade.sql").read_text("utf-8")
        assert "start_canary_upgrade" in text
        assert "complete_canary_upgrade" in text
        assert "promote_canary_to_production" in text


class TestReleaseAuditMigration:
    def test_048_exists(self):
        assert (_MIGRATIONS_DIR / "048_release_audit.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "048_release_audit.sql").read_text("utf-8")
        assert "database_release_audit" in text
        assert "release_id" in text
        assert "migration_set" in text
        assert "schema_hash" in text
        assert "backup_id" in text
        assert "certification_id" in text
        assert "result" in text
        assert "failure_reason" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "048_release_audit.sql").read_text("utf-8")
        assert "start_release_audit" in text
        assert "complete_release_audit" in text
        assert "get_release_audit_history" in text


class TestRollForwardMigration:
    def test_049_exists(self):
        assert (_MIGRATIONS_DIR / "049_roll_forward.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "049_roll_forward.sql").read_text("utf-8")
        assert "roll_forward_migration" in text
        assert "corrective_migration_id" in text
        assert "fixes_migration_id" in text
        assert "corrective_sql" in text
        assert "verification_sql" in text
        assert "verified" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "049_roll_forward.sql").read_text("utf-8")
        assert "record_roll_forward" in text
        assert "verify_roll_forward" in text
        assert "get_roll_forwards_for" in text


class TestQueryAllowlistPhaseF:
    def test_database_release_queries(self):
        assert is_allowlisted("database_release.active")
        assert is_allowlisted("database_release.list")

    def test_compatibility_query(self):
        assert is_allowlisted("release_compatibility.list")

    def test_migration_classification_queries(self):
        assert is_allowlisted("migration_classification.list")
        assert is_allowlisted("migration_classification.breaking")

    def test_query_contract_query(self):
        assert is_allowlisted("query_contract.active")

    def test_rls_role_migration_query(self):
        assert is_allowlisted("rls_role_migration.list")

    def test_sqlite_template_query(self):
        assert is_allowlisted("sqlite_template.active")

    def test_qdrant_contract_query(self):
        assert is_allowlisted("qdrant_contract.active")

    def test_canary_upgrade_query(self):
        assert is_allowlisted("canary_upgrade.list")

    def test_release_audit_query(self):
        assert is_allowlisted("release_audit.history")

    def test_roll_forward_query(self):
        assert is_allowlisted("roll_forward.list")


class TestSchemaContractRegistryPhaseF:
    def test_expected_migration_count_is_112(self):
        assert EXPECTED_MIGRATION_COUNT == 112

    def test_contract_includes_database_release(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "database_release") in table_names

    def test_contract_includes_release_compatibility(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "release_compatibility") in table_names

    def test_contract_includes_migration_classification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "migration_classification") in table_names

    def test_contract_includes_query_contract(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "query_contract") in table_names

    def test_contract_includes_rls_role_migration(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "rls_role_migration") in table_names

    def test_contract_includes_sqlite_template_release(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_template_release") in table_names

    def test_contract_includes_qdrant_contract(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_contract") in table_names

    def test_contract_includes_canary_upgrade(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "canary_upgrade") in table_names

    def test_contract_includes_database_release_audit(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "database_release_audit") in table_names

    def test_contract_includes_roll_forward_migration(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "roll_forward_migration") in table_names
