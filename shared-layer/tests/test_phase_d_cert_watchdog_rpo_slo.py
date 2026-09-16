"""Tests for Phase D: Rebuild Cert, Watchdog, RPO/RTO, Capacity, Read-Only Domain, Startup Cert, SLO.

Tests migration files 028-031, runtime helpers, and query allowlist extensions.
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


class TestRebuildCertificationMigration:
    def test_028_exists(self):
        assert (_MIGRATIONS_DIR / "028_rebuild_certification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "028_rebuild_certification.sql").read_text("utf-8")
        assert "gptbridge_index.rebuild_certification" in text
        assert "certified" in text
        assert "checks_performed" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "028_rebuild_certification.sql").read_text("utf-8")
        assert "record_rebuild_certification" in text
        assert "is_engine_certified" in text


class TestWatchdogBloatRpoRtoMigration:
    def test_029_exists(self):
        assert (_MIGRATIONS_DIR / "029_watchdog_bloat_rpo_rto.sql").is_file()

    def test_defines_watchdog_table(self):
        text = (_MIGRATIONS_DIR / "029_watchdog_bloat_rpo_rto.sql").read_text("utf-8")
        assert "long_transaction_watchdog" in text
        assert "transaction_age_seconds" in text
        assert "lock_holder" in text

    def test_defines_bloat_table(self):
        text = (_MIGRATIONS_DIR / "029_watchdog_bloat_rpo_rto.sql").read_text("utf-8")
        assert "bloat_report" in text
        assert "dead_tuples" in text
        assert "autovacuum_count" in text

    def test_defines_rpo_rto(self):
        text = (_MIGRATIONS_DIR / "029_watchdog_bloat_rpo_rto.sql").read_text("utf-8")
        assert "rpo_rto_class" in text
        assert "rpo_seconds" in text
        assert "rto_seconds" in text
        for engine in ("postgresql-central", "governance-codex-sqlite",
                       "module-sqlite", "qdrant"):
            assert engine in text

    def test_defines_capacity_thresholds(self):
        text = (_MIGRATIONS_DIR / "029_watchdog_bloat_rpo_rto.sql").read_text("utf-8")
        assert "capacity_threshold" in text
        assert "warning_level" in text
        assert "critical_level" in text
        assert "fail_closed_level" in text


class TestReadonlyDomainStartupCertMigration:
    def test_030_exists(self):
        assert (_MIGRATIONS_DIR / "030_readonly_domain_startup_cert.sql").is_file()

    def test_defines_readonly_domain(self):
        text = (_MIGRATIONS_DIR / "030_readonly_domain_startup_cert.sql").read_text("utf-8")
        assert "readonly_domain" in text
        assert "set_domain_readonly" in text
        assert "is_domain_readonly" in text

    def test_defines_startup_certification(self):
        text = (_MIGRATIONS_DIR / "030_readonly_domain_startup_cert.sql").read_text("utf-8")
        assert "startup_certification" in text
        assert "record_startup_certification" in text
        assert "is_database_ready" in text
        assert "schema_version_verified" in text
        assert "rls_verified" in text
        assert "audit_append_only_verified" in text
        assert "authority_contract_verified" in text


class TestSloMetricsMigration:
    def test_031_exists(self):
        assert (_MIGRATIONS_DIR / "031_slo_metrics.sql").is_file()

    def test_defines_slo_metric_table(self):
        text = (_MIGRATIONS_DIR / "031_slo_metrics.sql").read_text("utf-8")
        assert "slo_metric" in text
        assert "target_value" in text
        assert "target_direction" in text

    def test_defines_slo_observation_table(self):
        text = (_MIGRATIONS_DIR / "031_slo_metrics.sql").read_text("utf-8")
        assert "slo_observation" in text
        assert "record_slo_observation" in text

    def test_seeds_default_metrics(self):
        text = (_MIGRATIONS_DIR / "031_slo_metrics.sql").read_text("utf-8")
        for metric in ("central-query-p95", "transport-claim-latency",
                       "reconcile-backlog", "sqlite-lock-rate",
                       "qdrant-stale-rate", "restore-success"):
            assert metric in text


class TestRebuildCertifierModule:
    def test_import_certify(self):
        from shared_layer.database.rebuild_certifier import certify
        assert callable(certify)

    def test_import_is_certified(self):
        from shared_layer.database.rebuild_certifier import is_certified
        assert callable(is_certified)

    def test_lazy_export_via_init(self):
        from shared_layer.database import certify_rebuild, is_rebuild_certified
        assert callable(certify_rebuild)
        assert callable(is_rebuild_certified)


class TestWatchdogModule:
    def test_import_check_long_transactions(self):
        from shared_layer.database.watchdog import check_long_transactions
        assert callable(check_long_transactions)

    def test_import_collect_bloat_report(self):
        from shared_layer.database.watchdog import collect_bloat_report
        assert callable(collect_bloat_report)

    def test_import_get_rpo_rto_classes(self):
        from shared_layer.database.watchdog import get_rpo_rto_classes
        assert callable(get_rpo_rto_classes)

    def test_import_get_capacity_thresholds(self):
        from shared_layer.database.watchdog import get_capacity_thresholds
        assert callable(get_capacity_thresholds)

    def test_lazy_export_via_init(self):
        from shared_layer.database import check_long_transactions, collect_bloat_report
        assert callable(check_long_transactions)
        assert callable(collect_bloat_report)


class TestStartupCertifierModule:
    def test_import_certify_startup(self):
        from shared_layer.database.startup_certifier import certify_startup
        assert callable(certify_startup)

    def test_import_is_ready(self):
        from shared_layer.database.startup_certifier import is_ready
        assert callable(is_ready)

    def test_lazy_export_via_init(self):
        from shared_layer.database import certify_startup, is_database_ready
        assert callable(certify_startup)
        assert callable(is_database_ready)


class TestReadonlyDomainModule:
    def test_import_set_readonly(self):
        from shared_layer.database.readonly_domain import set_readonly
        assert callable(set_readonly)

    def test_import_is_readonly(self):
        from shared_layer.database.readonly_domain import is_readonly
        assert callable(is_readonly)

    def test_lazy_export_via_init(self):
        from shared_layer.database import set_domain_readonly, is_domain_readonly
        assert callable(set_domain_readonly)
        assert callable(is_domain_readonly)


class TestQueryAllowlistPhaseD:
    def test_rebuild_cert_queries(self):
        assert is_allowlisted("rebuild_cert.latest")
        assert is_allowlisted("rebuild_cert.by_engine")

    def test_watchdog_queries(self):
        assert is_allowlisted("watchdog.long_tx")
        assert is_allowlisted("bloat.latest")

    def test_rpo_rto_queries(self):
        assert is_allowlisted("rpo_rto.list")

    def test_capacity_queries(self):
        assert is_allowlisted("capacity.list")

    def test_readonly_domain_queries(self):
        assert is_allowlisted("readonly_domain.list")

    def test_startup_cert_queries(self):
        assert is_allowlisted("startup_cert.latest")

    def test_slo_queries(self):
        assert is_allowlisted("slo_metric.list")
        assert is_allowlisted("slo_observation.recent")


class TestSchemaContractRegistryPhaseD:
    def test_expected_migration_count_is_31(self):
        assert EXPECTED_MIGRATION_COUNT == 31

    def test_contract_includes_rebuild_certification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "rebuild_certification") in table_names

    def test_contract_includes_long_transaction_watchdog(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "long_transaction_watchdog") in table_names

    def test_contract_includes_bloat_report(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "bloat_report") in table_names

    def test_contract_includes_rpo_rto_class(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "rpo_rto_class") in table_names

    def test_contract_includes_capacity_threshold(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "capacity_threshold") in table_names

    def test_contract_includes_readonly_domain(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "readonly_domain") in table_names

    def test_contract_includes_startup_certification(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "startup_certification") in table_names

    def test_contract_includes_slo_metric(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "slo_metric") in table_names

    def test_contract_includes_slo_observation(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "slo_observation") in table_names
