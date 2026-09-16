"""Tests for data platform governance: invariants, contract registry,
migration compatibility, query allowlist, RLS matrix, SQLite fleet,
reconcile conflict classification, restore certification, dead-letter,
retention, backup catalog, generation fence, maintenance window.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure shared-layer/src is on sys.path
_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.database.migration_compatibility import (
    MigrationClassification,
    classify_migration,
    analyze_migration_directory,
)
from shared_layer.database.query_allowlist import (
    QUERY_TEMPLATES,
    get_query,
    is_allowlisted,
    validate_query,
)
from shared_layer.database.rls_test_matrix import (
    Expectation,
    declared_matrix,
    RlsTestCell,
)
from shared_layer.database.schema_contract_registry import (
    declared_contract,
    verify_contract,
    EXPECTED_MIGRATION_COUNT,
    TableContract,
)
from shared_layer.database.sqlite_fleet_registry import (
    discover_sqlite_databases,
    inspect_fleet,
    _inspect_one,
)
from shared_layer.database.restore_certification import (
    certify_restore,
    CertificationCheck,
)


# ============================================================================
# 1. Migration Compatibility
# ============================================================================

class TestMigrationCompatibility:
    def test_backward_compatible_add_column(self):
        sql = "ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar text DEFAULT '';"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BACKWARD_COMPATIBLE

    def test_breaking_drop_table(self):
        sql = "DROP TABLE foo;"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BREAKING
        assert "drops a table" in result.breaking_changes

    def test_breaking_drop_column(self):
        sql = "ALTER TABLE foo DROP COLUMN bar;"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BREAKING
        assert "drops a column" in result.breaking_changes

    def test_breaking_alter_type(self):
        sql = "ALTER TABLE foo ALTER COLUMN bar TYPE bigint;"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BREAKING

    def test_compatible_create_index(self):
        sql = "CREATE INDEX IF NOT EXISTS idx ON foo (bar);"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BACKWARD_COMPATIBLE

    def test_breaking_revoke(self):
        sql = "REVOKE SELECT ON foo FROM bar;"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.BREAKING

    def test_unknown_for_empty(self):
        sql = "-- just a comment"
        result = classify_migration(sql, "test.sql")
        assert result.classification == MigrationClassification.UNKNOWN

    def test_breaking_has_upgrade_notes(self):
        sql = "DROP TABLE foo;"
        result = classify_migration(sql, "test.sql")
        assert result.upgrade_notes != ""
        assert "upgrade path" in result.upgrade_notes

    def test_analyze_directory(self, tmp_path):
        (tmp_path / "001_add.sql").write_text(
            "ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar text;", encoding="utf-8"
        )
        (tmp_path / "002_drop.sql").write_text(
            "DROP TABLE foo;", encoding="utf-8"
        )
        results = analyze_migration_directory(tmp_path)
        assert len(results) == 2
        assert results[0].classification == MigrationClassification.BACKWARD_COMPATIBLE
        assert results[1].classification == MigrationClassification.BREAKING


# ============================================================================
# 2. Query Allowlist
# ============================================================================

class TestQueryAllowlist:
    def test_get_query_returns_template(self):
        sql = get_query("resource.get_by_id")
        assert "gptbridge_index.resource" in sql
        assert "%s" in sql

    def test_get_query_raises_for_unknown(self):
        with pytest.raises(KeyError, match="QUERY_NOT_ALLOWLISTED"):
            get_query("nonexistent.query")

    def test_is_allowlisted(self):
        assert is_allowlisted("resource.get_by_id") is True
        assert is_allowlisted("nonexistent") is False

    def test_templates_are_immutable(self):
        with pytest.raises(TypeError):
            QUERY_TEMPLATES["new.key"] = "SELECT 1"  # type: ignore

    def test_validate_query_matches(self):
        sql = get_query("resource.get_by_id")
        valid, key = validate_query(sql)
        assert valid is True
        assert key == "resource.get_by_id"

    def test_validate_query_rejects_unknown(self):
        valid, reason = validate_query("SELECT * FROM random_table")
        assert valid is False

    def test_all_transport_queries_exist(self):
        for key in (
            "transport.submit", "transport.claim", "transport.claim_update",
            "transport.respond", "transport.cancel", "transport.reclaim_expired",
            "transport.move_to_dead_letter", "transport.get_dead_letter",
        ):
            assert is_allowlisted(key), f"missing: {key}"

    def test_all_audit_queries_exist(self):
        for key in ("audit.insert", "audit.get_head", "audit.count_by_module"):
            assert is_allowlisted(key), f"missing: {key}"


# ============================================================================
# 3. Schema Contract Registry
# ============================================================================

class TestSchemaContractRegistry:
    def test_declared_contract_has_tables(self):
        contract = declared_contract()
        assert len(contract.tables) > 0
        table_names = [f"{t.schema}.{t.table}" for t in contract.tables]
        assert "gptbridge_index.resource" in table_names
        assert "gptbridge_transport.tool_request" in table_names
        assert "gptbridge_audit.event" in table_names

    def test_declared_contract_has_roles(self):
        contract = declared_contract()
        assert len(contract.roles) > 0
        role_names = [r.role_name for r in contract.roles]
        assert "gptbridge_index_reader" in role_names
        assert "gptbridge_index_executor" in role_names

    def test_expected_migration_count(self):
        migrations_dir = (
            Path(__file__).resolve().parents[1] / "migrations"
        )
        actual = len(list(migrations_dir.glob("*.sql")))
        assert EXPECTED_MIGRATION_COUNT == actual

    def test_resource_table_has_backend_generation(self):
        contract = declared_contract()
        resource = next(
            t for t in contract.tables
            if t.schema == "gptbridge_index" and t.table == "resource"
        )
        assert "backend_generation" in resource.columns
        assert "stale" in resource.columns

    def test_tool_request_has_dead_letter_columns(self):
        contract = declared_contract()
        transport = next(
            t for t in contract.tables
            if t.schema == "gptbridge_transport" and t.table == "tool_request"
        )
        assert "dead_letter_reason" in transport.columns
        assert "dead_letter_at" in transport.columns
        assert "max_attempts" in transport.columns

    def test_audit_event_has_sequence_number(self):
        contract = declared_contract()
        audit = next(
            t for t in contract.tables
            if t.schema == "gptbridge_audit" and t.table == "event"
        )
        assert "sequence_number" in audit.columns

    def test_verify_contract_with_missing_table(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        result = verify_contract(conn)
        assert result.passed is False
        assert any(d.issue == "table_missing" for d in result.drifts)

    def test_verify_contract_with_all_tables(self):
        contract = declared_contract()
        conn = MagicMock()
        # Mock: return columns for each table
        def mock_execute(sql, params=None):
            if "information_schema.columns" in sql:
                t = TableContract(
                    schema=params[0], table=params[1], columns=()
                )
                # Find the matching contract
                for tc in contract.tables:
                    if tc.schema == params[0] and tc.table == params[1]:
                        mock_cursor = MagicMock()
                        mock_cursor.fetchall.return_value = [
                            (c,) for c in tc.columns
                        ]
                        return mock_cursor
            elif "pg_class" in sql:
                mock_cursor = MagicMock()
                mock_cursor.fetchone.return_value = (True, True)  # rls on, forced
                return mock_cursor
            elif "gptbridge_migration.history" in sql:
                mock_cursor = MagicMock()
                mock_cursor.fetchone.return_value = (contract.migration_count,)
                return mock_cursor
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = None
            return mock_cursor
        conn.execute.side_effect = mock_execute
        result = verify_contract(conn)
        assert result.passed is True


# ============================================================================
# 4. RLS Test Matrix
# ============================================================================

class TestRlsTestMatrix:
    def test_declared_matrix_has_cells(self):
        matrix = declared_matrix()
        assert len(matrix) > 0
        # 4 roles × 13 tables × 4 operations = 208 cells
        assert len(matrix) == 4 * 13 * 4

    def test_reader_role_select_allowed(self):
        matrix = declared_matrix()
        for cell in matrix:
            if cell.role == "gptbridge_index_reader" and cell.operation == "SELECT":
                assert cell.expectation == Expectation.ALLOW

    def test_reader_role_write_denied(self):
        matrix = declared_matrix()
        for cell in matrix:
            if cell.role == "gptbridge_index_reader" and cell.operation in ("INSERT", "UPDATE", "DELETE"):
                assert cell.expectation == Expectation.DENY

    def test_transport_executor_only_transport(self):
        matrix = declared_matrix()
        for cell in matrix:
            if cell.role == "gptbridge_transport_executor":
                if cell.schema == "gptbridge_transport":
                    assert cell.expectation == Expectation.ALLOW
                elif cell.schema == "gptbridge_audit" and cell.operation == "INSERT":
                    assert cell.expectation == Expectation.ALLOW
                else:
                    assert cell.expectation == Expectation.DENY

    def test_index_executor_broad_access(self):
        matrix = declared_matrix()
        for cell in matrix:
            if cell.role == "gptbridge_index_executor":
                if cell.schema in ("gptbridge_index", "gptbridge_rag", "gptbridge_transport"):
                    assert cell.expectation == Expectation.ALLOW
                elif cell.schema == "gptbridge_audit" and cell.operation in ("SELECT", "INSERT"):
                    assert cell.expectation == Expectation.ALLOW
                else:
                    assert cell.expectation == Expectation.DENY


# ============================================================================
# 5. SQLite Fleet Registry
# ============================================================================

class TestSqliteFleetRegistry:
    def test_inspect_nonexistent_file(self, tmp_path):
        entry = _inspect_one(tmp_path / "nonexistent.db")
        assert entry.error == "file_not_found"
        assert entry.integrity_ok is False

    def test_inspect_valid_database(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (schema_version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("CREATE TABLE module_metadata (module_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO module_metadata VALUES ('test-module')")
        conn.commit()
        conn.close()
        entry = _inspect_one(db_path)
        assert entry.integrity_ok is True
        assert entry.schema_version == 1
        assert entry.owner == "test-module"

    def test_discover_databases(self, tmp_path):
        (tmp_path / "a.sqlite").write_text("")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.db").write_text("")
        (tmp_path / "c.txt").write_text("")
        found = discover_sqlite_databases([tmp_path])
        paths = [str(p) for p in found]
        assert any("a.sqlite" in p for p in paths)
        assert any("b.db" in p for p in paths)
        assert not any("c.txt" in p for p in paths)

    def test_inspect_fleet(self, tmp_path):
        db_path = tmp_path / "fleet.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (schema_version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (2)")
        conn.close()
        result = inspect_fleet([tmp_path])
        assert result.total_databases >= 1
        assert result.total_size_bytes > 0
        assert all(e.integrity_ok for e in result.entries if e.error == "")


# ============================================================================
# 6. Restore Certification
# ============================================================================

class TestRestoreCertification:
    def test_certify_with_all_passing(self):
        conn = MagicMock()
        def mock_execute(sql, params=None):
            mock_cursor = MagicMock()
            if "information_schema.columns" in sql:
                mock_cursor.fetchall.return_value = [("resource_id",)]
            elif "pg_class" in sql:
                mock_cursor.fetchall.return_value = []
            elif "count(*) FROM gptbridge_index.resource" in sql:
                mock_cursor.fetchone.return_value = (100,)
            elif "gptbridge_audit.event ORDER BY" in sql:
                mock_cursor.fetchone.return_value = ("abc-123",)
            elif "gptbridge_rag.index_state" in sql:
                mock_cursor.fetchone.return_value = (0,)
            elif "reconcile_conflict_log" in sql:
                mock_cursor.fetchone.return_value = (0,)
            elif "bump_backend_generation" in sql:
                mock_cursor.fetchone.return_value = (2,)
            elif "current_backend_generation" in sql:
                mock_cursor.fetchone.return_value = (1,)
            elif "gptbridge_migration.history" in sql:
                mock_cursor.fetchone.return_value = (17,)
            else:
                mock_cursor.fetchall.return_value = []
                mock_cursor.fetchone.return_value = None
            return mock_cursor
        conn.execute.side_effect = mock_execute
        result = certify_restore(conn, pre_restore_generation=1)
        assert result.generation_after == 2

    def test_certify_with_missing_qdrant_linkage(self):
        conn = MagicMock()
        def mock_execute(sql, params=None):
            mock_cursor = MagicMock()
            if "information_schema.columns" in sql:
                mock_cursor.fetchall.return_value = [("resource_id",)]
            elif "pg_class" in sql:
                mock_cursor.fetchall.return_value = []
            elif "count(*) FROM gptbridge_index.resource" in sql:
                mock_cursor.fetchone.return_value = (100,)
            elif "gptbridge_audit.event ORDER BY" in sql:
                mock_cursor.fetchone.return_value = ("abc-123",)
            elif "gptbridge_rag.index_state" in sql:
                mock_cursor.fetchone.return_value = (5,)  # 5 missing
            elif "reconcile_conflict_log" in sql:
                mock_cursor.fetchone.return_value = (0,)
            elif "bump_backend_generation" in sql:
                mock_cursor.fetchone.return_value = (2,)
            elif "current_backend_generation" in sql:
                mock_cursor.fetchone.return_value = (1,)
            elif "gptbridge_migration.history" in sql:
                mock_cursor.fetchone.return_value = (17,)
            else:
                mock_cursor.fetchall.return_value = []
                mock_cursor.fetchone.return_value = None
            return mock_cursor
        conn.execute.side_effect = mock_execute
        result = certify_restore(conn, pre_restore_generation=1)
        qdrant_check = next(c for c in result.checks if c.name == "qdrant_linkage")
        assert qdrant_check.passed is False

    def test_certify_to_dict(self):
        conn = MagicMock()
        conn.execute.return_value = MagicMock(
            fetchone=lambda: (0,),
            fetchall=lambda: [],
        )
        result = certify_restore(conn, pre_restore_generation=1, bump_generation=False)
        d = result.to_dict()
        assert "passed" in d
        assert "checks" in d
        assert "generation_before" in d
        assert "generation_after" in d


# ============================================================================
# 7. Dead-Letter Queue (SQLite-level test)
# ============================================================================

class TestDeadLetterQueue:
    def test_dead_letter_status_in_template(self):
        template = Path(__file__).resolve().parents[1] / "sql" / "sqlite_module_template.sql"
        # The SQLite template doesn't have dead-letter, but PostgreSQL does
        # Check the migration file exists
        migration = Path(__file__).resolve().parents[1] / "migrations" / "013_transport_dead_letter.sql"
        assert migration.is_file()
        content = migration.read_text(encoding="utf-8")
        assert "dead-letter" in content
        assert "dead_letter_reason" in content
        assert "max_attempts" in content


# ============================================================================
# 8. Reconcile Conflict Classification
# ============================================================================

class TestReconcileConflictClassification:
    def test_conflict_types_in_migration(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "014_reconcile_conflict_classification.sql"
        content = migration.read_text(encoding="utf-8")
        for conflict_type in (
            "revision_conflict",
            "missing_resource",
            "hash_mismatch",
            "deleted_remote",
            "schema_mismatch",
            "authorization_changed",
        ):
            assert conflict_type in content

    def test_conflict_type_in_sqlite_template(self):
        template = Path(__file__).resolve().parents[1] / "sql" / "sqlite_module_template.sql"
        content = template.read_text(encoding="utf-8")
        assert "conflict_type" in content
        assert "revision_conflict" in content


# ============================================================================
# 9. Retention Policy
# ============================================================================

class TestRetentionPolicy:
    def test_retention_migration_exists(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "015_retention_and_partition.sql"
        content = migration.read_text(encoding="utf-8")
        assert "retention_policy" in content
        assert "retention_days" in content
        assert "run_retention_purge" in content

    def test_default_retention_seeds(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "015_retention_and_partition.sql"
        content = migration.read_text(encoding="utf-8")
        assert "tool_request" in content
        assert "30" in content  # 30 days for transport
        assert "event" in content
        assert "90" in content  # 90 days for audit


# ============================================================================
# 10. Backup Catalog
# ============================================================================

class TestBackupCatalog:
    def test_backup_catalog_migration_exists(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "016_backup_catalog_and_generation_fence.sql"
        content = migration.read_text(encoding="utf-8")
        assert "backup_catalog" in content
        assert "backup_id" in content
        assert "source_generation" in content
        assert "schema_version" in content
        assert "backup_hash" in content
        assert "restore_tested_at" in content
        assert "restore_certified" in content

    def test_generation_fence_in_migration(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "016_backup_catalog_and_generation_fence.sql"
        content = migration.read_text(encoding="utf-8")
        assert "backend_generation_state" in content
        assert "enforce_generation_fence" in content
        assert "GENERATION_FENCE" in content
        assert "bump_backend_generation" in content


# ============================================================================
# 11. Maintenance Window
# ============================================================================

class TestMaintenanceWindow:
    def test_maintenance_window_migration_exists(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "017_maintenance_window.sql"
        content = migration.read_text(encoding="utf-8")
        assert "maintenance_window" in content
        assert "vacuum" in content
        assert "analyze" in content
        assert "index-rebuild" in content
        assert "maintenance_window_active" in content


# ============================================================================
# 12. Cross-Engine Consistency
# ============================================================================

class TestCrossEngineConsistency:
    def test_consistency_migration_exists(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "012_cross_engine_consistency.sql"
        content = migration.read_text(encoding="utf-8")
        assert "backend_generation" in content
        assert "stale" in content
        assert "resource_consistency" in content
        assert "consistency_status" in content

    def test_sqlite_template_has_consistency_columns(self):
        template = Path(__file__).resolve().parents[1] / "sql" / "sqlite_module_template.sql"
        content = template.read_text(encoding="utf-8")
        assert "backend_generation" in content
        assert "stale" in content


# ============================================================================
# 13. Data Invariants
# ============================================================================

class TestDataInvariants:
    def test_invariants_migration_exists(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "011_data_invariants.sql"
        content = migration.read_text(encoding="utf-8")
        assert "enforce_status_transition" in content
        assert "assign_sequence" in content
        assert "enforce_version_monotonic" in content
        assert "verify_chunk_index_state" in content
        assert "INV_VIOLATION" in content
        assert "sequence_number" in content
