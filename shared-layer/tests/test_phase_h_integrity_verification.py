"""Tests for Phase H: Integrity Verification.

Tests migrations 064-073, runtime helpers, and query allowlist.
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

class TestAuditHashChainMigration:
    def test_064_exists(self):
        assert (_MIGRATIONS_DIR / "064_audit_hash_chain.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "064_audit_hash_chain.sql").read_text("utf-8")
        assert "event_hash" in text
        assert "previous_event_hash" in text
        assert "sequence" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "064_audit_hash_chain.sql").read_text("utf-8")
        assert "compute_event_hash" in text
        assert "populate_event_hash_chain" in text
        assert "verify_audit_chain" in text
        assert "get_audit_head_hash" in text

class TestReconcileBatchDigestMigration:
    def test_065_exists(self):
        assert (_MIGRATIONS_DIR / "065_reconcile_batch_digest.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "065_reconcile_batch_digest.sql").read_text("utf-8")
        assert "reconcile_batch_digest" in text
        assert "batch_hash" in text
        assert "result_hash" in text
        # Additional table items
        assert "source_generation" in text
        assert "first_revision" in text
        assert "last_revision" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "065_reconcile_batch_digest.sql").read_text("utf-8")
        assert "start_reconcile_batch" in text
        assert "complete_reconcile_batch" in text
        assert "verify_reconcile_batch" in text

class TestResourceContentHashMigration:
    def test_066_exists(self):
        assert (_MIGRATIONS_DIR / "066_resource_content_hash.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "066_resource_content_hash.sql").read_text("utf-8")
        assert "resource_content_hash" in text
        assert "resource_hash" in text
        assert "metadata_hash" in text
        # Additional table items
        assert "locator_hash" in text
        assert "revision" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "066_resource_content_hash.sql").read_text("utf-8")
        assert "record_resource_hash" in text
        assert "verify_resource_hash" in text
        assert "get_tampered_resources" in text

class TestSqliteDatabaseDigestMigration:
    def test_067_exists(self):
        assert (_MIGRATIONS_DIR / "067_sqlite_database_digest.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "067_sqlite_database_digest.sql").read_text("utf-8")
        assert "sqlite_database_digest" in text
        assert "schema_hash" in text
        assert "revision_head" in text
        # Additional table items
        assert "row_count" in text
        assert "critical_table_digest" in text
        assert "generation" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "067_sqlite_database_digest.sql").read_text("utf-8")
        assert "record_sqlite_digest" in text
        assert "verify_sqlite_digest" in text
        assert "get_tampered_sqlite_dbs" in text

class TestQdrantIntegrityMappingMigration:
    def test_068_exists(self):
        assert (_MIGRATIONS_DIR / "068_qdrant_integrity_mapping.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "068_qdrant_integrity_mapping.sql").read_text("utf-8")
        assert "qdrant_integrity_map" in text
        assert "chunk_hash" in text
        assert "embedding_version" in text
        # Additional table items
        assert "qdrant_point_id" in text
        assert "resource_revision" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "068_qdrant_integrity_mapping.sql").read_text("utf-8")
        assert "record_qdrant_integrity" in text
        assert "verify_qdrant_integrity" in text
        assert "get_qdrant_integrity_issues" in text

class TestMerkleRootMigration:
    def test_069_exists(self):
        assert (_MIGRATIONS_DIR / "069_merkle_root.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "069_merkle_root.sql").read_text("utf-8")
        assert "merkle_root" in text
        assert "leaf_count" in text
        assert "leaf_hashes" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "069_merkle_root.sql").read_text("utf-8")
        assert "compute_merkle_root" in text
        assert "record_merkle_root" in text
        assert "verify_merkle_root" in text
        assert "get_merkle_root_for_domain" in text

class TestIntegritySnapshotMigration:
    def test_070_exists(self):
        assert (_MIGRATIONS_DIR / "070_integrity_snapshot.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "070_integrity_snapshot.sql").read_text("utf-8")
        assert "integrity_snapshot" in text
        assert "schema_hash" in text
        assert "audit_head_hash" in text
        # Additional table items
        assert "resource_merkle_root" in text
        assert "migration_head" in text
        assert "database_generation" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "070_integrity_snapshot.sql").read_text("utf-8")
        assert "create_integrity_snapshot" in text
        assert "get_latest_snapshot" in text

class TestRestoreVerificationMigration:
    def test_071_exists(self):
        assert (_MIGRATIONS_DIR / "071_restore_verification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "071_restore_verification.sql").read_text("utf-8")
        assert "restore_verification" in text
        assert "expected_schema_hash" in text
        assert "actual_schema_hash" in text
        # Additional table items
        assert "schema_match" in text
        assert "audit_match" in text
        assert "merkle_match" in text
        assert "overall_passed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "071_restore_verification.sql").read_text("utf-8")
        assert "record_restore_verification" in text
        assert "get_failed_restores" in text

class TestTamperStateMigration:
    def test_072_exists(self):
        assert (_MIGRATIONS_DIR / "072_tamper_state.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "072_tamper_state.sql").read_text("utf-8")
        assert "tamper_state_registry" in text

    def test_defines_all_states(self):
        text = (_MIGRATIONS_DIR / "072_tamper_state.sql").read_text("utf-8")
        for state in ("verified", "unverified", "mismatch", "tampered",
                      "incomplete", "rebuild_required"):
            assert state in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "072_tamper_state.sql").read_text("utf-8")
        assert "record_tamper_state" in text
        assert "resolve_tamper_state" in text
        assert "get_active_tamper_issues" in text

class TestFailClosedMigration:
    def test_073_exists(self):
        assert (_MIGRATIONS_DIR / "073_fail_closed.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "073_fail_closed.sql").read_text("utf-8")
        assert "fail_closed_action" in text

    def test_defines_trigger_types(self):
        text = (_MIGRATIONS_DIR / "073_fail_closed.sql").read_text("utf-8")
        for trigger in ("codex_hash_mismatch", "audit_chain_broken",
                        "schema_contract_mismatch", "restore_snapshot_mismatch"):
            assert trigger in text

    def test_defines_actions(self):
        text = (_MIGRATIONS_DIR / "073_fail_closed.sql").read_text("utf-8")
        for action in ("read_only", "quarantine", "recovery"):
            assert action in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "073_fail_closed.sql").read_text("utf-8")
        assert "trigger_fail_closed" in text
        assert "release_fail_closed" in text
        assert "is_fail_closed_active" in text
        assert "get_active_fail_closed" in text

class TestIntegrityVerifierModule:
    def test_import_compute_hash(self):
        from shared_layer.database.integrity_verifier import compute_hash
        assert callable(compute_hash)

    def test_import_compute_merkle_root(self):
        from shared_layer.database.integrity_verifier import compute_merkle_root
        assert callable(compute_merkle_root)

    def test_import_populate_event_hash_chain(self):
        from shared_layer.database.integrity_verifier import populate_event_hash_chain
        assert callable(populate_event_hash_chain)

    def test_import_verify_audit_chain(self):
        from shared_layer.database.integrity_verifier import verify_audit_chain
        assert callable(verify_audit_chain)

    def test_import_get_audit_head_hash(self):
        from shared_layer.database.integrity_verifier import get_audit_head_hash
        assert callable(get_audit_head_hash)

    def test_import_start_reconcile_batch(self):
        from shared_layer.database.integrity_verifier import start_reconcile_batch
        assert callable(start_reconcile_batch)

    def test_import_complete_reconcile_batch(self):
        from shared_layer.database.integrity_verifier import complete_reconcile_batch
        assert callable(complete_reconcile_batch)

    def test_import_record_resource_hash(self):
        from shared_layer.database.integrity_verifier import record_resource_hash
        assert callable(record_resource_hash)

    def test_import_verify_resource_hash(self):
        from shared_layer.database.integrity_verifier import verify_resource_hash
        assert callable(verify_resource_hash)

    def test_import_record_sqlite_digest(self):
        from shared_layer.database.integrity_verifier import record_sqlite_digest
        assert callable(record_sqlite_digest)

    def test_import_record_qdrant_integrity(self):
        from shared_layer.database.integrity_verifier import record_qdrant_integrity
        assert callable(record_qdrant_integrity)

    def test_import_verify_qdrant_integrity(self):
        from shared_layer.database.integrity_verifier import verify_qdrant_integrity
        assert callable(verify_qdrant_integrity)

    def test_import_record_merkle_root(self):
        from shared_layer.database.integrity_verifier import record_merkle_root
        assert callable(record_merkle_root)

    def test_import_create_integrity_snapshot(self):
        from shared_layer.database.integrity_verifier import create_integrity_snapshot
        assert callable(create_integrity_snapshot)

    def test_import_record_restore_verification(self):
        from shared_layer.database.integrity_verifier import record_restore_verification
        assert callable(record_restore_verification)

    def test_import_record_tamper_state(self):
        from shared_layer.database.integrity_verifier import record_tamper_state
        assert callable(record_tamper_state)

    def test_import_trigger_fail_closed(self):
        from shared_layer.database.integrity_verifier import trigger_fail_closed
        assert callable(trigger_fail_closed)

    def test_import_is_fail_closed_active(self):
        from shared_layer.database.integrity_verifier import is_fail_closed_active
        assert callable(is_fail_closed_active)

    def test_lazy_export_via_init(self):
        from shared_layer.database import populate_event_hash_chain, verify_audit_chain
        assert callable(populate_event_hash_chain)
        assert callable(verify_audit_chain)

    def test_compute_hash_deterministic(self):
        from shared_layer.database.integrity_verifier import compute_hash
        h1 = compute_hash("a", "b", "c")
        h2 = compute_hash("a", "b", "c")
        h3 = compute_hash("a", "b", "d")
        assert h1 == h2
        assert h1 != h3

    def test_compute_merkle_root_single_leaf(self):
        from shared_layer.database.integrity_verifier import compute_merkle_root
        root = compute_merkle_root(["abc"])
        assert root == "abc"

    def test_compute_merkle_root_two_leaves(self):
        import hashlib
        from shared_layer.database.integrity_verifier import compute_merkle_root
        leaf1, leaf2 = "abc", "def"
        expected = hashlib.sha256((leaf1 + leaf2).encode()).hexdigest()
        root = compute_merkle_root([leaf1, leaf2])
        assert root == expected

    def test_compute_merkle_root_empty(self):
        from shared_layer.database.integrity_verifier import compute_merkle_root
        assert compute_merkle_root([]) is None

class TestQueryAllowlistPhaseH:
    def test_audit_hash_chain_queries(self):
        assert is_allowlisted("audit_hash_chain.verify")
        assert is_allowlisted("audit_hash_chain.head")

    def test_reconcile_batch_query(self):
        assert is_allowlisted("reconcile_batch.list")

    def test_resource_content_hash_queries(self):
        assert is_allowlisted("resource_content_hash.list")
        assert is_allowlisted("resource_content_hash.tampered")

    def test_sqlite_digest_queries(self):
        assert is_allowlisted("sqlite_digest.list")
        assert is_allowlisted("sqlite_digest.tampered")

    def test_qdrant_integrity_query(self):
        assert is_allowlisted("qdrant_integrity.issues")

    def test_merkle_root_query(self):
        assert is_allowlisted("merkle_root.list")

    def test_integrity_snapshot_query(self):
        assert is_allowlisted("integrity_snapshot.latest")

    def test_restore_verification_query(self):
        assert is_allowlisted("restore_verification.recent")

    def test_tamper_state_query(self):
        assert is_allowlisted("tamper_state.active")

    def test_fail_closed_query(self):
        assert is_allowlisted("fail_closed.active")

class TestSchemaContractRegistryPhaseH:
    def test_expected_migration_count_is_125(self):
        import pathlib

        migrations = pathlib.Path(__file__).resolve().parents[1] / "migrations"
        assert EXPECTED_MIGRATION_COUNT == len(list(migrations.glob("*.sql")))

    def test_contract_includes_reconcile_batch_digest(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "reconcile_batch_digest") in table_names

    def test_contract_includes_resource_content_hash(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "resource_content_hash") in table_names

    def test_contract_includes_sqlite_database_digest(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_database_digest") in table_names

    def test_contract_includes_qdrant_integrity_map(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "qdrant_integrity_map") in table_names

    def test_contract_includes_merkle_root(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "merkle_root") in table_names

    def test_contract_includes_integrity_snapshot(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "integrity_snapshot") in table_names

    def test_contract_includes_restore_verification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "restore_verification") in table_names

    def test_contract_includes_tamper_state_registry(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "tamper_state_registry") in table_names

    def test_contract_includes_fail_closed_action(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "fail_closed_action") in table_names
