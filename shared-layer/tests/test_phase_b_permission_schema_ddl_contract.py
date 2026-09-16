"""Tests for Phase B: Permission Snapshot, Schema Lock, DDL Audit, Contract Handshake.

Tests migration files 021-024, runtime helpers, and query allowlist extensions.
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


class TestPermissionSnapshotMigration:
    def test_021_exists(self):
        assert (_MIGRATIONS_DIR / "021_permission_snapshot.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "021_permission_snapshot.sql").read_text("utf-8")
        assert "gptbridge_audit.permission_snapshot" in text
        assert "evaluated_roles" in text
        assert "evaluated_policies" in text
        assert "decision_summary" in text
        assert "rls_context" in text

    def test_defines_capture_function(self):
        text = (_MIGRATIONS_DIR / "021_permission_snapshot.sql").read_text("utf-8")
        assert "capture_permission_snapshot" in text
        assert "can_write_resource" in text


class TestSchemaOwnershipLockMigration:
    def test_022_exists(self):
        assert (_MIGRATIONS_DIR / "022_schema_ownership_lock.sql").is_file()

    def test_defines_migration_owner(self):
        text = (_MIGRATIONS_DIR / "022_schema_ownership_lock.sql").read_text("utf-8")
        assert "gptbridge_migration_owner" in text

    def test_defines_ddl_guard(self):
        text = (_MIGRATIONS_DIR / "022_schema_ownership_lock.sql").read_text("utf-8")
        assert "ddl_guard" in text
        assert "ddl_guard_trigger" in text
        assert "is_migration_executor" in text


class TestDDLAuditMigration:
    def test_023_exists(self):
        assert (_MIGRATIONS_DIR / "023_ddl_audit.sql").is_file()

    def test_defines_ddl_event_table(self):
        text = (_MIGRATIONS_DIR / "023_ddl_audit.sql").read_text("utf-8")
        assert "gptbridge_audit.ddl_event" in text
        assert "command_tag" in text
        assert "object_identity" in text
        assert "migration_executor" in text

    def test_defines_audit_trigger(self):
        text = (_MIGRATIONS_DIR / "023_ddl_audit.sql").read_text("utf-8")
        assert "audit_ddl_event" in text
        assert "ddl_audit_trigger" in text


class TestContractHandshakeMigration:
    def test_024_exists(self):
        assert (_MIGRATIONS_DIR / "024_contract_version_handshake.sql").is_file()

    def test_defines_contract_version_table(self):
        text = (_MIGRATIONS_DIR / "024_contract_version_handshake.sql").read_text("utf-8")
        assert "gptbridge_index.contract_version" in text
        assert "current_version" in text
        assert "min_compatible_version" in text

    def test_defines_compatibility_check(self):
        text = (_MIGRATIONS_DIR / "024_contract_version_handshake.sql").read_text("utf-8")
        assert "check_contract_compatibility" in text
        assert "enforce_contract_version" in text
        assert "contract_version_fence" in text

    def test_seeds_central_index_contract(self):
        text = (_MIGRATIONS_DIR / "024_contract_version_handshake.sql").read_text("utf-8")
        assert "central-index" in text


class TestPermissionSnapshotHelper:
    def test_import_capture_snapshot(self):
        from shared_layer.database.permission_snapshot import capture_snapshot
        assert callable(capture_snapshot)

    def test_lazy_export_via_init(self):
        from shared_layer.database import capture_snapshot
        assert callable(capture_snapshot)


class TestProvenanceHelpers:
    def test_set_contract_version(self):
        from shared_layer.database.provenance import set_contract_version
        assert callable(set_contract_version)

    def test_set_migration_executor(self):
        from shared_layer.database.provenance import set_migration_executor
        assert callable(set_migration_executor)

    def test_clear_migration_executor(self):
        from shared_layer.database.provenance import clear_migration_executor
        assert callable(clear_migration_executor)


class TestQueryAllowlistPhaseB:
    def test_contract_version_queries(self):
        assert is_allowlisted("contract_version.get")
        assert is_allowlisted("contract_version.list")
        assert is_allowlisted("contract_version.check_compatible")

    def test_permission_snapshot_queries(self):
        assert is_allowlisted("permission_snapshot.get_by_event")
        assert is_allowlisted("permission_snapshot.get_by_actor")

    def test_ddl_audit_queries(self):
        assert is_allowlisted("ddl_audit.recent")
        assert is_allowlisted("ddl_audit.by_schema")


class TestSchemaContractRegistryPhaseB:
    def test_expected_migration_count_is_31(self):
        assert EXPECTED_MIGRATION_COUNT == 31

    def test_contract_includes_permission_snapshot(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_audit", "permission_snapshot") in table_names

    def test_contract_includes_ddl_event(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_audit", "ddl_event") in table_names

    def test_contract_includes_contract_version(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "contract_version") in table_names
