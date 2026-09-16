"""Tests for Phase K: Data Layer Contract.

Tests migrations 114-125, runtime helpers, and query allowlist.
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

class TestDataLayerContractMigration:
    def test_114_exists(self):
        assert (_MIGRATIONS_DIR / "114_data_layer_contract.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "114_data_layer_contract.sql").read_text("utf-8")
        assert "data_layer_contract" in text
        assert "contract_version" in text
        assert "startup_order" in text
        assert "shutdown_order" in text
        assert "degradation_policy" in text
        assert "recovery_policy" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "114_data_layer_contract.sql").read_text("utf-8")
        assert "register_data_layer_contract" in text
        assert "activate_data_layer_contract" in text
        assert "get_active_data_layer_contract" in text

class TestDependencyClassificationMigration:
    def test_115_exists(self):
        assert (_MIGRATIONS_DIR / "115_dependency_classification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "115_dependency_classification.sql").read_text("utf-8")
        assert "dependency_classification" in text
        assert "authority" in text
        assert "required" in text
        assert "degradable" in text
        assert "optional" in text
        assert "postgresql" in text
        assert "sqlite_codex" in text
        assert "qdrant" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "115_dependency_classification.sql").read_text("utf-8")
        assert "classify_dependency" in text
        assert "get_dependency_classification" in text

class TestStartupPhaseMigration:
    def test_116_exists(self):
        assert (_MIGRATIONS_DIR / "116_startup_phase.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "116_startup_phase.sql").read_text("utf-8")
        assert "startup_phase" in text
        assert "BOOTSTRAP" in text
        assert "GOVERNANCE_VALIDATED" in text
        assert "DATABASE_FOUNDATION_READY" in text
        assert "CENTRAL_AUTHORITY_READY" in text
        assert "PRIVATE_STATE_READY" in text
        assert "SEMANTIC_INDEX_READY" in text
        assert "RECOVERY_READY" in text
        assert "READ_MODELS_READY" in text
        assert "CORE_READY" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "116_startup_phase.sql").read_text("utf-8")
        assert "get_startup_order" in text

class TestStartupPhaseGateMigration:
    def test_117_exists(self):
        assert (_MIGRATIONS_DIR / "117_startup_phase_gate.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "117_startup_phase_gate.sql").read_text("utf-8")
        assert "startup_phase_gate" in text
        assert "governance_ready" in text
        assert "security_ready" in text
        assert "authority_ready" in text
        assert "audit_ready" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "117_startup_phase_gate.sql").read_text("utf-8")
        assert "register_startup_gate" in text
        assert "set_gate_result" in text
        assert "is_phase_complete" in text
        assert "can_enable_write" in text

class TestSchemaReadinessMigration:
    def test_118_exists(self):
        assert (_MIGRATIONS_DIR / "118_schema_readiness.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "118_schema_readiness.sql").read_text("utf-8")
        assert "schema_readiness" in text
        assert "gptbridge_index" in text
        assert "gptbridge_transport" in text
        assert "gptbridge_audit" in text
        assert "gptbridge_rag" in text
        assert "gptbridge_identity" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "118_schema_readiness.sql").read_text("utf-8")
        assert "set_schema_readiness" in text
        assert "is_schema_ready" in text
        assert "is_pg_certified" in text
        assert "is_audit_writable" in text
        assert "can_enable_business_write" in text

class TestRagReadinessGateMigration:
    def test_119_exists(self):
        assert (_MIGRATIONS_DIR / "119_rag_readiness_gate.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "119_rag_readiness_gate.sql").read_text("utf-8")
        assert "rag_readiness_gate" in text
        assert "pg_rag_metadata_ready" in text
        assert "qdrant_ready" in text
        assert "metadata_authority_wired" in text
        assert "collection_contract_valid" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "119_rag_readiness_gate.sql").read_text("utf-8")
        assert "evaluate_rag_readiness" in text
        assert "is_rag_ready" in text

class TestShutdownPhaseMigration:
    def test_120_exists(self):
        assert (_MIGRATIONS_DIR / "120_shutdown_phase.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "120_shutdown_phase.sql").read_text("utf-8")
        assert "shutdown_phase" in text
        assert "STOP_ACCEPTING_NEW_WORK" in text
        assert "DRAIN_TRANSPORT" in text
        assert "FLUSH_AUDIT" in text
        assert "CLOSE_QDRANT_CLIENT" in text
        assert "CLOSE_SQLITE" in text
        assert "CLOSE_POSTGRES_POOLS" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "120_shutdown_phase.sql").read_text("utf-8")
        assert "get_shutdown_order" in text

class TestShutdownAuditMigration:
    def test_121_exists(self):
        assert (_MIGRATIONS_DIR / "121_shutdown_audit.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "121_shutdown_audit.sql").read_text("utf-8")
        assert "shutdown_audit" in text
        assert "shutdown_status" in text
        assert "graceful" in text
        assert "unclean" in text
        assert "drain_result" in text
        assert "database_generation" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "121_shutdown_audit.sql").read_text("utf-8")
        assert "start_shutdown_audit" in text
        assert "complete_shutdown_audit" in text
        assert "was_last_shutdown_graceful" in text

class TestUncleanShutdownDetectionMigration:
    def test_122_exists(self):
        assert (_MIGRATIONS_DIR / "122_unclean_shutdown_detection.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "122_unclean_shutdown_detection.sql").read_text("utf-8")
        assert "unclean_shutdown_detection" in text
        assert "transport_lease_recovery" in text
        assert "unknown_commit_verification" in text
        assert "sqlite_wal_verification" in text
        assert "reconcile_state_verification" in text
        assert "operation_state_recovery" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "122_unclean_shutdown_detection.sql").read_text("utf-8")
        assert "detect_unclean_shutdown" in text
        assert "mark_unclean_step_done" in text
        assert "is_unclean_recovery_complete" in text

class TestCacheInvalidationPolicyMigration:
    def test_123_exists(self):
        assert (_MIGRATIONS_DIR / "123_cache_invalidation_policy.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "123_cache_invalidation_policy.sql").read_text("utf-8")
        assert "cache_invalidation_policy" in text
        assert "check_generation_compatible" in text
        assert "check_revision_compatible" in text
        assert "check_ttl_valid" in text
        assert "on_mismatch" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "123_cache_invalidation_policy.sql").read_text("utf-8")
        assert "register_cache_policy" in text
        assert "should_invalidate_cache" in text

class TestDataLayerDependencyGraphMigration:
    def test_124_exists(self):
        assert (_MIGRATIONS_DIR / "124_data_layer_dependency_graph.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "124_data_layer_dependency_graph.sql").read_text("utf-8")
        assert "data_layer_dependency_graph" in text
        assert "governance_codex" in text
        assert "identity_permission" in text
        assert "postgresql" in text
        assert "structured_authority" in text
        assert "semantic_canonical" in text
        assert "bounded_local_state" in text
        assert "non_canonical" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "124_data_layer_dependency_graph.sql").read_text("utf-8")
        assert "get_dependencies" in text
        assert "get_dependents" in text

class TestIntegrationRuleMigration:
    def test_125_exists(self):
        assert (_MIGRATIONS_DIR / "125_integration_rule.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "125_integration_rule.sql").read_text("utf-8")
        assert "integration_rule" in text
        assert "central structured authority" in text
        assert "canonical semantic index" in text
        assert "durable workflow" in text
        assert "bounded" in text
        assert "rebuildable" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "125_integration_rule.sql").read_text("utf-8")
        assert "get_integration_rules" in text
        assert "check_integration_rule" in text

class TestDataLayerContractModule:
    def test_import_register_data_layer_contract(self):
        from shared_layer.database.data_layer_contract import register_data_layer_contract
        assert callable(register_data_layer_contract)

    def test_import_activate_data_layer_contract(self):
        from shared_layer.database.data_layer_contract import activate_data_layer_contract
        assert callable(activate_data_layer_contract)

    def test_import_get_active_data_layer_contract(self):
        from shared_layer.database.data_layer_contract import get_active_data_layer_contract
        assert callable(get_active_data_layer_contract)

    def test_import_classify_dependency(self):
        from shared_layer.database.data_layer_contract import classify_dependency
        assert callable(classify_dependency)

    def test_import_get_startup_order(self):
        from shared_layer.database.data_layer_contract import get_startup_order
        assert callable(get_startup_order)

    def test_import_get_shutdown_order(self):
        from shared_layer.database.data_layer_contract import get_shutdown_order
        assert callable(get_shutdown_order)

    def test_import_register_startup_gate(self):
        from shared_layer.database.data_layer_contract import register_startup_gate
        assert callable(register_startup_gate)

    def test_import_set_gate_result(self):
        from shared_layer.database.data_layer_contract import set_gate_result
        assert callable(set_gate_result)

    def test_import_is_phase_complete(self):
        from shared_layer.database.data_layer_contract import is_phase_complete
        assert callable(is_phase_complete)

    def test_import_can_enable_write(self):
        from shared_layer.database.data_layer_contract import can_enable_write
        assert callable(can_enable_write)

    def test_import_set_schema_readiness(self):
        from shared_layer.database.data_layer_contract import set_schema_readiness
        assert callable(set_schema_readiness)

    def test_import_is_schema_ready(self):
        from shared_layer.database.data_layer_contract import is_schema_ready
        assert callable(is_schema_ready)

    def test_import_is_pg_certified(self):
        from shared_layer.database.data_layer_contract import is_pg_certified
        assert callable(is_pg_certified)

    def test_import_can_enable_business_write(self):
        from shared_layer.database.data_layer_contract import can_enable_business_write
        assert callable(can_enable_business_write)

    def test_import_evaluate_rag_readiness(self):
        from shared_layer.database.data_layer_contract import evaluate_rag_readiness
        assert callable(evaluate_rag_readiness)

    def test_import_is_rag_ready(self):
        from shared_layer.database.data_layer_contract import is_rag_ready
        assert callable(is_rag_ready)

    def test_import_start_shutdown_audit(self):
        from shared_layer.database.data_layer_contract import start_shutdown_audit
        assert callable(start_shutdown_audit)

    def test_import_complete_shutdown_audit(self):
        from shared_layer.database.data_layer_contract import complete_shutdown_audit
        assert callable(complete_shutdown_audit)

    def test_import_was_last_shutdown_graceful(self):
        from shared_layer.database.data_layer_contract import was_last_shutdown_graceful
        assert callable(was_last_shutdown_graceful)

    def test_import_detect_unclean_shutdown(self):
        from shared_layer.database.data_layer_contract import detect_unclean_shutdown
        assert callable(detect_unclean_shutdown)

    def test_import_mark_unclean_step_done(self):
        from shared_layer.database.data_layer_contract import mark_unclean_step_done
        assert callable(mark_unclean_step_done)

    def test_import_is_unclean_recovery_complete(self):
        from shared_layer.database.data_layer_contract import is_unclean_recovery_complete
        assert callable(is_unclean_recovery_complete)

    def test_import_register_cache_policy(self):
        from shared_layer.database.data_layer_contract import register_cache_policy
        assert callable(register_cache_policy)

    def test_import_should_invalidate_cache(self):
        from shared_layer.database.data_layer_contract import should_invalidate_cache
        assert callable(should_invalidate_cache)

    def test_import_get_dependencies(self):
        from shared_layer.database.data_layer_contract import get_dependencies
        assert callable(get_dependencies)

    def test_import_get_dependents(self):
        from shared_layer.database.data_layer_contract import get_dependents
        assert callable(get_dependents)

    def test_import_get_integration_rules(self):
        from shared_layer.database.data_layer_contract import get_integration_rules
        assert callable(get_integration_rules)

    def test_import_check_integration_rule(self):
        from shared_layer.database.data_layer_contract import check_integration_rule
        assert callable(check_integration_rule)

    def test_lazy_export_via_init(self):
        from shared_layer.database import get_startup_order, is_pg_certified
        assert callable(get_startup_order)
        assert callable(is_pg_certified)

class TestQueryAllowlistPhaseK:
    def test_data_layer_contract_query(self):
        assert is_allowlisted("data_layer_contract.active")

    def test_dependency_classification_query(self):
        assert is_allowlisted("dependency_classification.list")

    def test_startup_phase_query(self):
        assert is_allowlisted("startup_phase.order")

    def test_startup_gate_query(self):
        assert is_allowlisted("startup_gate.list")

    def test_schema_readiness_query(self):
        assert is_allowlisted("schema_readiness.list")

    def test_rag_readiness_query(self):
        assert is_allowlisted("rag_readiness.latest")

    def test_shutdown_phase_query(self):
        assert is_allowlisted("shutdown_phase.order")

    def test_shutdown_audit_query(self):
        assert is_allowlisted("shutdown_audit.latest")

    def test_unclean_shutdown_query(self):
        assert is_allowlisted("unclean_shutdown.latest")

    def test_cache_invalidation_query(self):
        assert is_allowlisted("cache_invalidation_policy.list")

    def test_dependency_graph_query(self):
        assert is_allowlisted("dependency_graph.edges")

    def test_integration_rule_query(self):
        assert is_allowlisted("integration_rule.list")

class TestSchemaContractRegistryPhaseK:
    def test_expected_migration_count_is_125(self):
        assert EXPECTED_MIGRATION_COUNT == 125

    def test_contract_includes_data_layer_contract(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "data_layer_contract") in table_names

    def test_contract_includes_dependency_classification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "dependency_classification") in table_names

    def test_contract_includes_startup_phase(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "startup_phase") in table_names

    def test_contract_includes_startup_phase_gate(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "startup_phase_gate") in table_names

    def test_contract_includes_schema_readiness(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "schema_readiness") in table_names

    def test_contract_includes_rag_readiness_gate(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "rag_readiness_gate") in table_names

    def test_contract_includes_shutdown_phase(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "shutdown_phase") in table_names

    def test_contract_includes_shutdown_audit(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "shutdown_audit") in table_names

    def test_contract_includes_unclean_shutdown_detection(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "unclean_shutdown_detection") in table_names

    def test_contract_includes_cache_invalidation_policy(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "cache_invalidation_policy") in table_names

    def test_contract_includes_data_layer_dependency_graph(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "data_layer_dependency_graph") in table_names

    def test_contract_includes_integration_rule(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "integration_rule") in table_names
