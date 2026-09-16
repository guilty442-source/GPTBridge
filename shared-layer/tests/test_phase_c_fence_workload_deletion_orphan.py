"""Tests for Phase C: Generation Fence, Workload Class, Two-Stage Deletion, Orphan Scanner.

Tests migration files 025-027, runtime helpers, and query allowlist extensions.
"""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.database.query_allowlist import get_query, is_allowlisted
from shared_layer.database.schema_contract_registry import (
    declared_contract,
    EXPECTED_MIGRATION_COUNT,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = _PROJECT_ROOT / "shared-layer" / "migrations"


class TestSqliteGenerationFenceMigration:
    def test_025_exists(self):
        assert (_MIGRATIONS_DIR / "025_sqlite_generation_fence.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "025_sqlite_generation_fence.sql").read_text("utf-8")
        assert "gptbridge_index.sqlite_generation" in text
        assert "backend_generation" in text
        assert "stale" in text

    def test_defines_upsert_function(self):
        text = (_MIGRATIONS_DIR / "025_sqlite_generation_fence.sql").read_text("utf-8")
        assert "upsert_sqlite_generation" in text


class TestWorkloadClassMigration:
    def test_026_exists(self):
        assert (_MIGRATIONS_DIR / "026_workload_class.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "026_workload_class.sql").read_text("utf-8")
        assert "gptbridge_index.workload_class" in text
        assert "statement_timeout_ms" in text
        assert "lock_timeout_ms" in text
        assert "priority" in text

    def test_seeds_six_classes(self):
        text = (_MIGRATIONS_DIR / "026_workload_class.sql").read_text("utf-8")
        for cls in ("interactive", "transport", "audit",
                     "reconciliation", "maintenance", "migration"):
            assert cls in text

    def test_defines_apply_function(self):
        text = (_MIGRATIONS_DIR / "026_workload_class.sql").read_text("utf-8")
        assert "apply_workload_class" in text


class TestTwoStageDeletionMigration:
    def test_027_exists(self):
        assert (_MIGRATIONS_DIR / "027_two_stage_deletion.sql").is_file()

    def test_defines_deletion_stage(self):
        text = (_MIGRATIONS_DIR / "027_two_stage_deletion.sql").read_text("utf-8")
        assert "deletion_stage" in text
        for stage in ("active", "tombstone", "retention", "purged"):
            assert stage in text

    def test_defines_tombstone_function(self):
        text = (_MIGRATIONS_DIR / "027_two_stage_deletion.sql").read_text("utf-8")
        assert "tombstone_resource" in text
        assert "tombstoned_at" in text
        assert "purge_after" in text

    def test_defines_advance_function(self):
        text = (_MIGRATIONS_DIR / "027_two_stage_deletion.sql").read_text("utf-8")
        assert "advance_deletion_stage" in text

    def test_defines_purge_eligible(self):
        text = (_MIGRATIONS_DIR / "027_two_stage_deletion.sql").read_text("utf-8")
        assert "get_purge_eligible" in text


class TestGenerationFenceHelper:
    def test_import_get_current_generation(self):
        from shared_layer.database.generation_fence import get_current_generation
        assert callable(get_current_generation)

    def test_import_bump_generation(self):
        from shared_layer.database.generation_fence import bump_generation
        assert callable(bump_generation)

    def test_import_is_connection_stale(self):
        from shared_layer.database.generation_fence import is_connection_stale
        assert callable(is_connection_stale)

    def test_lazy_export_via_init(self):
        from shared_layer.database import get_current_generation, bump_generation
        assert callable(get_current_generation)
        assert callable(bump_generation)


class TestDeletionCoordinator:
    def test_import_tombstone(self):
        from shared_layer.database.deletion_coordinator import tombstone
        assert callable(tombstone)

    def test_import_advance_stage(self):
        from shared_layer.database.deletion_coordinator import advance_stage
        assert callable(advance_stage)

    def test_import_get_purge_eligible(self):
        from shared_layer.database.deletion_coordinator import get_purge_eligible
        assert callable(get_purge_eligible)

    def test_lazy_export_via_init(self):
        from shared_layer.database import tombstone, advance_stage
        assert callable(tombstone)
        assert callable(advance_stage)


class TestOrphanScanner:
    def test_import_scan_orphans(self):
        from shared_layer.database.orphan_scanner import scan_orphans
        assert callable(scan_orphans)

    def test_lazy_export_via_init(self):
        from shared_layer.database import scan_orphans
        assert callable(scan_orphans)


class TestQueryAllowlistPhaseC:
    def test_workload_class_queries(self):
        assert is_allowlisted("workload_class.get")
        assert is_allowlisted("workload_class.list")

    def test_generation_queries(self):
        assert is_allowlisted("generation.sqlite_stale")

    def test_deletion_queries(self):
        assert is_allowlisted("deletion.tombstone")
        assert is_allowlisted("deletion.advance")
        assert is_allowlisted("deletion.purge_eligible")
        assert is_allowlisted("deletion.by_stage")


class TestSchemaContractRegistryPhaseC:
    def test_expected_migration_count_is_85(self):
        assert EXPECTED_MIGRATION_COUNT == 85

    def test_contract_includes_sqlite_generation(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_generation") in table_names

    def test_contract_includes_workload_class(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "workload_class") in table_names

    def test_resource_has_deletion_stage(self):
        contract = declared_contract()
        resource = next(t for t in contract.tables if t.table == "resource")
        assert "deletion_stage" in resource.columns
        assert "tombstoned_at" in resource.columns
        assert "purge_after" in resource.columns

    def test_index_state_has_deletion_stage(self):
        contract = declared_contract()
        index_state = next(t for t in contract.tables if t.table == "index_state")
        assert "deletion_stage" in index_state.columns
