"""Tests for Phase A: Data Lineage, Authority Marker, Write Provenance.

Tests the migration files (018-020), the runtime provenance helper,
the query allowlist extensions, and the SQLite template updates.
"""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.database.query_allowlist import (
    get_query,
    is_allowlisted,
)
from shared_layer.database.schema_contract_registry import (
    declared_contract,
    EXPECTED_MIGRATION_COUNT,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _PROJECT_ROOT / "shared-layer" / "migrations"


class TestMigrationFiles:
    def test_018_data_lineage_exists(self):
        assert (_MIGRATIONS_DIR / "018_data_lineage.sql").is_file()

    def test_019_authority_marker_exists(self):
        assert (_MIGRATIONS_DIR / "019_authority_marker.sql").is_file()

    def test_020_write_provenance_exists(self):
        assert (_MIGRATIONS_DIR / "020_write_provenance.sql").is_file()

    def test_018_defines_data_lineage_table(self):
        text = (_MIGRATIONS_DIR / "018_data_lineage.sql").read_text("utf-8")
        assert "gptbridge_index.data_lineage" in text
        assert "source_module" in text
        assert "source_revision" in text
        assert "produce_method" in text
        assert "sync_path" in text
        assert "last_writer_id" in text

    def test_018_defines_lineage_trigger(self):
        text = (_MIGRATIONS_DIR / "018_data_lineage.sql").read_text("utf-8")
        assert "auto_populate_lineage" in text
        assert "resource_lineage_populate" in text

    def test_018_defines_resource_lineage_view(self):
        text = (_MIGRATIONS_DIR / "018_data_lineage.sql").read_text("utf-8")
        assert "resource_lineage" in text
        assert "CREATE OR REPLACE VIEW" in text

    def test_019_defines_authority_class(self):
        text = (_MIGRATIONS_DIR / "019_authority_marker.sql").read_text("utf-8")
        assert "authority_class" in text
        for cls in ("central-official", "module-private", "derived", "cache", "degraded-copy"):
            assert cls in text

    def test_020_defines_provenance_columns(self):
        text = (_MIGRATIONS_DIR / "020_write_provenance.sql").read_text("utf-8")
        assert "executor_id" in text
        assert "correlation_id" in text
        assert "source_revision" in text

    def test_020_defines_provenance_triggers(self):
        text = (_MIGRATIONS_DIR / "020_write_provenance.sql").read_text("utf-8")
        assert "auto_populate_provenance" in text
        assert "resource_provenance_populate" in text
        assert "audit_event_provenance_populate" in text


class TestSqliteTemplate:
    def test_template_has_authority_class(self):
        template = (_PROJECT_ROOT / "shared-layer" / "sql" / "sqlite_module_template.sql").read_text("utf-8")
        assert "authority_class" in template
        assert "central-official" in template
        assert "module-private" in template
        assert "degraded-copy" in template

    def test_template_has_provenance_columns(self):
        template = (_PROJECT_ROOT / "shared-layer" / "sql" / "sqlite_module_template.sql").read_text("utf-8")
        assert "executor_id" in template
        assert "correlation_id" in template
        assert "source_revision" in template


class TestQueryAllowlist:
    def test_lineage_queries_registered(self):
        assert is_allowlisted("lineage.get_by_resource")
        assert is_allowlisted("lineage.get_by_correlation")
        assert is_allowlisted("lineage.resource_lineage_view")

    def test_resource_get_by_id_includes_authority_class(self):
        sql = get_query("resource.get_by_id")
        assert "authority_class" in sql
        assert "executor_id" in sql
        assert "correlation_id" in sql
        assert "source_revision" in sql

    def test_resource_insert_includes_authority_class(self):
        sql = get_query("resource.insert")
        assert "authority_class" in sql

    def test_resource_upsert_includes_authority_class(self):
        sql = get_query("resource.upsert")
        assert "authority_class" in sql

    def test_index_state_upsert_includes_authority_class(self):
        sql = get_query("rag.index_state.upsert")
        assert "authority_class" in sql

    def test_lineage_get_by_resource_query(self):
        sql = get_query("lineage.get_by_resource")
        assert "gptbridge_index.data_lineage" in sql
        assert "source_module" in sql
        assert "last_writer_correlation_id" in sql

    def test_lineage_resource_lineage_view_query(self):
        sql = get_query("lineage.resource_lineage_view")
        assert "resource_lineage" in sql
        assert "authority_class" in sql
        assert "qdrant_consistency" in sql


class TestProvenanceHelper:
    def test_import_set_provenance(self):
        from shared_layer.database.provenance import set_provenance
        assert callable(set_provenance)

    def test_import_clear_provenance(self):
        from shared_layer.database.provenance import clear_provenance
        assert callable(clear_provenance)

    def test_lazy_export_via_init(self):
        from shared_layer.database import set_provenance, clear_provenance
        assert callable(set_provenance)
        assert callable(clear_provenance)


class TestSchemaContractRegistry:
    def test_expected_migration_count_is_125(self):
        import pathlib

        migrations = pathlib.Path(__file__).resolve().parents[1] / "migrations"
        assert EXPECTED_MIGRATION_COUNT == len(list(migrations.glob("*.sql")))

    def test_contract_includes_data_lineage_table(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "data_lineage") in table_names

    def test_resource_contract_has_authority_class(self):
        contract = declared_contract()
        resource = next(t for t in contract.tables if t.table == "resource")
        assert "authority_class" in resource.columns
        assert "executor_id" in resource.columns
        assert "correlation_id" in resource.columns
        assert "source_revision" in resource.columns

    def test_index_state_contract_has_authority_class(self):
        contract = declared_contract()
        index_state = next(t for t in contract.tables if t.table == "index_state")
        assert "authority_class" in index_state.columns
        assert "executor_id" in index_state.columns

    def test_audit_event_contract_has_provenance(self):
        contract = declared_contract()
        event = next(t for t in contract.tables if t.table == "event")
        assert "executor_id" in event.columns
        assert "correlation_id" in event.columns
        assert "source_revision" in event.columns
