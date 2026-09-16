"""Tests for Phase I: Dependency & Version Governance.

Tests migrations 074-085, runtime helpers, and query allowlist.
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

class TestVersionLockMigration:
    def test_074_exists(self):
        assert (_MIGRATIONS_DIR / "074_version_lock.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "074_version_lock.sql").read_text("utf-8")
        assert "version_lock" in text
        assert "component" in text
        assert "version_string" in text
        assert "major_version" in text
        assert "minor_version" in text
        assert "patch_version" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "074_version_lock.sql").read_text("utf-8")
        assert "lock_version" in text
        assert "get_version_lock" in text
        assert "get_all_version_locks" in text

class TestCompatibilityMatrixExtMigration:
    def test_075_exists(self):
        assert (_MIGRATIONS_DIR / "075_compatibility_matrix_ext.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "075_compatibility_matrix_ext.sql").read_text("utf-8")
        assert "compatibility_matrix_ext" in text
        assert "postgresql_version" in text
        assert "psycopg_version" in text
        assert "sqlite_runtime_version" in text
        assert "qdrant_server_version" in text
        assert "status" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "075_compatibility_matrix_ext.sql").read_text("utf-8")
        assert "record_compatibility" in text
        assert "check_combination_allowed" in text
        assert "get_forbidden_combinations" in text

class TestUpgradeClassificationMigration:
    def test_076_exists(self):
        assert (_MIGRATIONS_DIR / "076_upgrade_classification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "076_upgrade_classification.sql").read_text("utf-8")
        assert "upgrade_classification" in text
        assert "upgrade_class" in text
        assert "required_validation" in text
        assert "allows_unattended" in text
        assert "requires_backup" in text
        assert "requires_clone_test" in text
        assert "requires_certification" in text
        assert "rollback_allowed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "076_upgrade_classification.sql").read_text("utf-8")
        assert "classify_upgrade" in text
        assert "get_upgrade_class" in text

class TestDriverCompatibilityTestMigration:
    def test_077_exists(self):
        assert (_MIGRATIONS_DIR / "077_driver_compatibility_test.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "077_driver_compatibility_test.sql").read_text("utf-8")
        assert "driver_compatibility_test" in text
        assert "driver_name" in text
        assert "driver_version" in text
        assert "test_category" in text
        assert "passed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "077_driver_compatibility_test.sql").read_text("utf-8")
        assert "record_driver_test" in text
        assert "is_driver_version_verified" in text
        assert "get_failed_driver_tests" in text

class TestPgMajorUpgradeRehearsalMigration:
    def test_078_exists(self):
        assert (_MIGRATIONS_DIR / "078_pg_major_upgrade_rehearsal.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "078_pg_major_upgrade_rehearsal.sql").read_text("utf-8")
        assert "pg_major_upgrade_rehearsal" in text
        assert "from_version" in text
        assert "to_version" in text
        assert "status" in text
        assert "backup_id" in text
        assert "migration_check_passed" in text
        assert "rls_check_passed" in text
        assert "transport_test_passed" in text
        assert "reconcile_test_passed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "078_pg_major_upgrade_rehearsal.sql").read_text("utf-8")
        assert "start_pg_rehearsal" in text
        assert "advance_pg_rehearsal" in text
        assert "get_rehearsal_summary" in text

class TestSqliteRuntimeCompatMigration:
    def test_079_exists(self):
        assert (_MIGRATIONS_DIR / "079_sqlite_runtime_compat.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "079_sqlite_runtime_compat.sql").read_text("utf-8")
        assert "sqlite_runtime_compat" in text
        assert "python_version" in text
        assert "sqlite_library_version" in text
        assert "fts5_available" in text
        assert "wal_mode_available" in text
        assert "json1_available" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "079_sqlite_runtime_compat.sql").read_text("utf-8")
        assert "record_sqlite_runtime_compat" in text
        assert "check_sqlite_runtime_compat" in text

class TestQdrantContractCompatMigration:
    def test_080_exists(self):
        assert (_MIGRATIONS_DIR / "080_qdrant_contract_compat.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "080_qdrant_contract_compat.sql").read_text("utf-8")
        assert "qdrant_contract_compat" in text
        assert "from_version" in text
        assert "to_version" in text
        assert "check_category" in text
        assert "passed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "080_qdrant_contract_compat.sql").read_text("utf-8")
        assert "record_qdrant_compat" in text
        assert "is_qdrant_upgrade_safe" in text

class TestSbomDependencyInventoryMigration:
    def test_081_exists(self):
        assert (_MIGRATIONS_DIR / "081_sbom_dependency_inventory.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "081_sbom_dependency_inventory.sql").read_text("utf-8")
        assert "sbom_dependency_inventory" in text
        assert "component" in text
        assert "component_type" in text
        assert "version" in text
        assert "source" in text
        assert "source_hash" in text
        assert "install_path" in text
        assert "verified" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "081_sbom_dependency_inventory.sql").read_text("utf-8")
        assert "record_sbom_entry" in text
        assert "verify_sbom_entry" in text
        assert "get_sbom_for_release" in text

class TestVulnerabilityRiskMigration:
    def test_082_exists(self):
        assert (_MIGRATIONS_DIR / "082_vulnerability_risk.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "082_vulnerability_risk.sql").read_text("utf-8")
        assert "vulnerability_risk" in text
        assert "component" in text
        assert "affected_versions" in text
        assert "risk_level" in text
        assert "recommended_action" in text
        assert "critical_security" in text
        assert "important" in text
        assert "compatible_maintenance" in text
        assert "optional" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "082_vulnerability_risk.sql").read_text("utf-8")
        assert "record_vulnerability" in text
        assert "resolve_vulnerability" in text
        assert "get_critical_vulnerabilities" in text

class TestDependencyDriftMigration:
    def test_083_exists(self):
        assert (_MIGRATIONS_DIR / "083_dependency_drift.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "083_dependency_drift.sql").read_text("utf-8")
        assert "dependency_drift" in text
        assert "component" in text
        assert "expected_version" in text
        assert "installed_version" in text
        assert "drift_status" in text
        assert "UNVERIFIED_DEPENDENCY" in text
        assert "MISMATCH" in text
        assert "VERIFIED" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "083_dependency_drift.sql").read_text("utf-8")
        assert "record_dependency_drift" in text
        assert "resolve_dependency_drift" in text
        assert "get_unverified_dependencies" in text

class TestOfflineBundleMigration:
    def test_084_exists(self):
        assert (_MIGRATIONS_DIR / "084_offline_bundle.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "084_offline_bundle.sql").read_text("utf-8")
        assert "offline_bundle" in text
        assert "component" in text
        assert "version" in text
        assert "package_type" in text
        assert "storage_locator" in text
        assert "file_hash" in text
        assert "verified" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "084_offline_bundle.sql").read_text("utf-8")
        assert "register_offline_bundle" in text
        assert "verify_offline_bundle" in text
        assert "get_offline_bundle" in text

class TestReleaseSignatureMigration:
    def test_085_exists(self):
        assert (_MIGRATIONS_DIR / "085_release_signature.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "085_release_signature.sql").read_text("utf-8")
        assert "release_signature" in text
        assert "bundle_hash" in text
        assert "component_count" in text
        assert "component_hashes" in text
        assert "tamper_state" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "085_release_signature.sql").read_text("utf-8")
        assert "sign_release" in text
        assert "verify_release_signature" in text
        assert "get_latest_signature" in text

class TestDependencyGovernorModule:
    def test_import_compute_bundle_hash(self):
        from shared_layer.database.dependency_governor import compute_bundle_hash
        assert callable(compute_bundle_hash)

    def test_import_lock_version(self):
        from shared_layer.database.dependency_governor import lock_version
        assert callable(lock_version)

    def test_import_get_version_lock(self):
        from shared_layer.database.dependency_governor import get_version_lock
        assert callable(get_version_lock)

    def test_import_record_compatibility(self):
        from shared_layer.database.dependency_governor import record_compatibility
        assert callable(record_compatibility)

    def test_import_check_combination_allowed(self):
        from shared_layer.database.dependency_governor import check_combination_allowed
        assert callable(check_combination_allowed)

    def test_import_classify_upgrade(self):
        from shared_layer.database.dependency_governor import classify_upgrade
        assert callable(classify_upgrade)

    def test_import_get_upgrade_class(self):
        from shared_layer.database.dependency_governor import get_upgrade_class
        assert callable(get_upgrade_class)

    def test_import_record_driver_test(self):
        from shared_layer.database.dependency_governor import record_driver_test
        assert callable(record_driver_test)

    def test_import_is_driver_version_verified(self):
        from shared_layer.database.dependency_governor import is_driver_version_verified
        assert callable(is_driver_version_verified)

    def test_import_start_pg_rehearsal(self):
        from shared_layer.database.dependency_governor import start_pg_rehearsal
        assert callable(start_pg_rehearsal)

    def test_import_advance_pg_rehearsal(self):
        from shared_layer.database.dependency_governor import advance_pg_rehearsal
        assert callable(advance_pg_rehearsal)

    def test_import_record_sqlite_runtime_compat(self):
        from shared_layer.database.dependency_governor import record_sqlite_runtime_compat
        assert callable(record_sqlite_runtime_compat)

    def test_import_record_qdrant_compat(self):
        from shared_layer.database.dependency_governor import record_qdrant_compat
        assert callable(record_qdrant_compat)

    def test_import_record_sbom_entry(self):
        from shared_layer.database.dependency_governor import record_sbom_entry
        assert callable(record_sbom_entry)

    def test_import_record_vulnerability(self):
        from shared_layer.database.dependency_governor import record_vulnerability
        assert callable(record_vulnerability)

    def test_import_record_dependency_drift(self):
        from shared_layer.database.dependency_governor import record_dependency_drift
        assert callable(record_dependency_drift)

    def test_import_register_offline_bundle(self):
        from shared_layer.database.dependency_governor import register_offline_bundle
        assert callable(register_offline_bundle)

    def test_import_sign_release(self):
        from shared_layer.database.dependency_governor import sign_release
        assert callable(sign_release)

    def test_import_verify_release_signature(self):
        from shared_layer.database.dependency_governor import verify_release_signature
        assert callable(verify_release_signature)

    def test_lazy_export_via_init(self):
        from shared_layer.database import lock_version, classify_upgrade
        assert callable(lock_version)
        assert callable(classify_upgrade)

    def test_compute_bundle_hash_deterministic(self):
        from shared_layer.database.dependency_governor import compute_bundle_hash
        components = [{"component": "pg", "hash": "abc"}, {"component": "psycopg", "hash": "def"}]
        h1 = compute_bundle_hash(components)
        h2 = compute_bundle_hash(components)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

class TestQueryAllowlistPhaseI:
    def test_version_lock_query(self):
        assert is_allowlisted("version_lock.list")

    def test_compat_matrix_ext_query(self):
        assert is_allowlisted("compat_matrix_ext.list")
        assert is_allowlisted("compat_matrix_ext.forbidden")

    def test_upgrade_classification_query(self):
        assert is_allowlisted("upgrade_classification.list")

    def test_driver_compat_query(self):
        assert is_allowlisted("driver_compat.failed")

    def test_pg_rehearsal_query(self):
        assert is_allowlisted("pg_rehearsal.list")

    def test_sqlite_runtime_compat_query(self):
        assert is_allowlisted("sqlite_runtime_compat.list")

    def test_qdrant_compat_query(self):
        assert is_allowlisted("qdrant_compat.list")

    def test_sbom_query(self):
        assert is_allowlisted("sbom.list")

    def test_vulnerability_query(self):
        assert is_allowlisted("vulnerability.critical")

    def test_dependency_drift_query(self):
        assert is_allowlisted("dependency_drift.unverified")

    def test_offline_bundle_query(self):
        assert is_allowlisted("offline_bundle.list")

    def test_release_signature_query(self):
        assert is_allowlisted("release_signature.latest")

class TestSchemaContractRegistryPhaseI:
    def test_expected_migration_count_is_85(self):
        assert EXPECTED_MIGRATION_COUNT == 85

    def test_contract_includes_version_lock(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "version_lock") in table_names

    def test_contract_includes_compatibility_matrix_ext(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "compatibility_matrix_ext") in table_names

    def test_contract_includes_upgrade_classification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "upgrade_classification") in table_names

    def test_contract_includes_driver_compatibility_test(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "driver_compatibility_test") in table_names

    def test_contract_includes_pg_major_upgrade_rehearsal(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "pg_major_upgrade_rehearsal") in table_names

    def test_contract_includes_sqlite_runtime_compat(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_runtime_compat") in table_names

    def test_contract_includes_qdrant_contract_compat(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_contract_compat") in table_names

    def test_contract_includes_sbom_dependency_inventory(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sbom_dependency_inventory") in table_names

    def test_contract_includes_vulnerability_risk(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "vulnerability_risk") in table_names

    def test_contract_includes_dependency_drift(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "dependency_drift") in table_names

    def test_contract_includes_offline_bundle(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "offline_bundle") in table_names

    def test_contract_includes_release_signature(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "release_signature") in table_names
