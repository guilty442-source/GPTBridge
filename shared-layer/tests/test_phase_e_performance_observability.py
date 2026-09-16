"""Tests for Phase E: Performance and Observability.

Tests migrations 032-039, runtime helpers, and query allowlist extensions.
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


class TestWorkloadPoolQueryClassMigration:
    def test_032_exists(self):
        assert (_MIGRATIONS_DIR / "032_workload_pool_query_class.sql").is_file()

    def test_defines_workload_pool_config(self):
        text = (_MIGRATIONS_DIR / "032_workload_pool_query_class.sql").read_text("utf-8")
        assert "workload_pool_config" in text
        assert "max_connections" in text
        for pool in ("index", "transport", "audit", "reconcile", "maintenance"):
            assert pool in text

    def test_defines_query_class(self):
        text = (_MIGRATIONS_DIR / "032_workload_pool_query_class.sql").read_text("utf-8")
        assert "query_class" in text
        assert "statement_timeout_ms" in text
        assert "lock_timeout_ms" in text
        assert "retry_limit" in text
        assert "batch_size" in text
        assert "priority" in text
        for cls in ("interactive", "transport", "index_lookup", "audit_write",
                    "reconcile", "migration", "maintenance"):
            assert cls in text

    def test_defines_apply_query_class(self):
        text = (_MIGRATIONS_DIR / "032_workload_pool_query_class.sql").read_text("utf-8")
        assert "apply_query_class" in text


class TestQueryFingerprintMigration:
    def test_033_exists(self):
        assert (_MIGRATIONS_DIR / "033_query_fingerprint.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "033_query_fingerprint.sql").read_text("utf-8")
        assert "query_fingerprint" in text
        assert "execution_count" in text
        assert "mean_latency_ms" in text
        assert "p95_latency_ms" in text
        assert "rows_returned_total" in text
        assert "shared_blocks_hit_total" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "033_query_fingerprint.sql").read_text("utf-8")
        assert "record_query_fingerprint" in text
        assert "get_hot_queries" in text


class TestTransportHotPathIndexMigration:
    def test_034_exists(self):
        assert (_MIGRATIONS_DIR / "034_transport_hot_path_index.sql").is_file()

    def test_defines_composite_index(self):
        text = (_MIGRATIONS_DIR / "034_transport_hot_path_index.sql").read_text("utf-8")
        assert "tool_request_claim_path_idx" in text
        assert "channel_id" in text
        assert "target_tool_id" in text
        assert "status" in text

    def test_defines_history_table(self):
        text = (_MIGRATIONS_DIR / "034_transport_hot_path_index.sql").read_text("utf-8")
        assert "tool_request_history" in text
        assert "archive_completed_requests" in text


class TestAuditHotHistoryMigration:
    def test_035_exists(self):
        assert (_MIGRATIONS_DIR / "035_audit_hot_history_separation.sql").is_file()

    def test_defines_history_table(self):
        text = (_MIGRATIONS_DIR / "035_audit_hot_history_separation.sql").read_text("utf-8")
        assert "event_history" in text
        assert "archive_audit_events" in text

    def test_defines_partition_threshold(self):
        text = (_MIGRATIONS_DIR / "035_audit_hot_history_separation.sql").read_text("utf-8")
        assert "partition_threshold" in text
        assert "row_count_threshold" in text
        assert "size_mb_threshold" in text


class TestWalCheckpointMonitorMigration:
    def test_036_exists(self):
        assert (_MIGRATIONS_DIR / "036_wal_checkpoint_monitor.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "036_wal_checkpoint_monitor.sql").read_text("utf-8")
        assert "wal_checkpoint_snapshot" in text
        assert "checkpoint_duration_ms" in text
        assert "wal_rate_mb_per_min" in text

    def test_defines_function(self):
        text = (_MIGRATIONS_DIR / "036_wal_checkpoint_monitor.sql").read_text("utf-8")
        assert "record_wal_checkpoint_snapshot" in text


class TestSQLiteClassificationMigration:
    def test_037_exists(self):
        assert (_MIGRATIONS_DIR / "037_sqlite_classification.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "037_sqlite_classification.sql").read_text("utf-8")
        assert "sqlite_database_class" in text
        assert "synchronous_setting" in text
        assert "backup_frequency_seconds" in text
        assert "integrity_check_frequency_seconds" in text
        assert "retention_days" in text
        assert "reconcile_required" in text

    def test_defines_function(self):
        text = (_MIGRATIONS_DIR / "037_sqlite_classification.sql").read_text("utf-8")
        assert "upsert_sqlite_class" in text


class TestIncrementalReconcileMigration:
    def test_038_exists(self):
        assert (_MIGRATIONS_DIR / "038_incremental_reconcile.sql").is_file()

    def test_defines_queue_table(self):
        text = (_MIGRATIONS_DIR / "038_incremental_reconcile.sql").read_text("utf-8")
        assert "reconcile_pending_queue" in text
        assert "dirty" in text
        assert "source_revision" in text
        assert "last_reconciled_revision" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "038_incremental_reconcile.sql").read_text("utf-8")
        assert "enqueue_reconcile_pending" in text
        assert "mark_reconciled" in text
        assert "get_pending_reconcile" in text
        assert "purge_reconciled" in text


class TestPerformanceBaselineMigration:
    def test_039_exists(self):
        assert (_MIGRATIONS_DIR / "039_performance_baseline.sql").is_file()

    def test_defines_table(self):
        text = (_MIGRATIONS_DIR / "039_performance_baseline.sql").read_text("utf-8")
        assert "performance_baseline" in text
        assert "p50_latency_ms" in text
        assert "p95_latency_ms" in text
        assert "p99_latency_ms" in text
        assert "sample_count" in text

    def test_defines_functions(self):
        text = (_MIGRATIONS_DIR / "039_performance_baseline.sql").read_text("utf-8")
        assert "record_baseline" in text
        assert "get_latest_baseline" in text
        assert "compare_baseline" in text


class TestQueryFingerprintModule:
    def test_import_record(self):
        from shared_layer.database.query_fingerprint import record
        assert callable(record)

    def test_import_get_hot(self):
        from shared_layer.database.query_fingerprint import get_hot
        assert callable(get_hot)

    def test_lazy_export_via_init(self):
        from shared_layer.database import record_query_fingerprint, get_hot_queries
        assert callable(record_query_fingerprint)
        assert callable(get_hot_queries)


class TestSqlitePragmaPolicyModule:
    def test_import_apply_pragma(self):
        from shared_layer.database.sqlite_pragma_policy import apply_pragma
        assert callable(apply_pragma)

    def test_import_get_pragma_policy(self):
        from shared_layer.database.sqlite_pragma_policy import get_pragma_policy
        assert callable(get_pragma_policy)

    def test_class_a_is_conservative(self):
        from shared_layer.database.sqlite_pragma_policy import get_pragma_policy
        policy = get_pragma_policy("A")
        assert policy["synchronous"] == "FULL"
        assert policy["journal_mode"] == "WAL"

    def test_class_b_is_normal(self):
        from shared_layer.database.sqlite_pragma_policy import get_pragma_policy
        policy = get_pragma_policy("B")
        assert policy["synchronous"] == "NORMAL"

    def test_all_four_classes(self):
        from shared_layer.database.sqlite_pragma_policy import get_all_policies
        policies = get_all_policies()
        assert set(policies.keys()) == {"A", "B", "C", "D"}

    def test_lazy_export_via_init(self):
        from shared_layer.database import apply_sqlite_pragma, get_sqlite_pragma_policy
        assert callable(apply_sqlite_pragma)
        assert callable(get_sqlite_pragma_policy)


class TestSqliteClassificationModule:
    def test_import_register(self):
        from shared_layer.database.sqlite_classification import register
        assert callable(register)

    def test_import_get_class(self):
        from shared_layer.database.sqlite_classification import get_class
        assert callable(get_class)

    def test_import_list_by_class(self):
        from shared_layer.database.sqlite_classification import list_by_class
        assert callable(list_by_class)

    def test_lazy_export_via_init(self):
        from shared_layer.database import register_sqlite_class, get_sqlite_class
        assert callable(register_sqlite_class)
        assert callable(get_sqlite_class)


class TestSqliteWalGovernorModule:
    def test_import_check_and_checkpoint(self):
        from shared_layer.database.sqlite_wal_governor import check_and_checkpoint
        assert callable(check_and_checkpoint)

    def test_import_get_wal_stats(self):
        from shared_layer.database.sqlite_wal_governor import get_wal_stats
        assert callable(get_wal_stats)

    def test_lazy_export_via_init(self):
        from shared_layer.database import check_and_checkpoint, get_wal_stats
        assert callable(check_and_checkpoint)
        assert callable(get_wal_stats)


class TestBatchWriterModule:
    def test_import_batch_writer(self):
        from shared_layer.database.batch_writer import BatchWriter
        assert BatchWriter is not None

    def test_batch_writer_has_add(self):
        from shared_layer.database.batch_writer import BatchWriter
        assert hasattr(BatchWriter, "add")

    def test_batch_writer_has_flush(self):
        from shared_layer.database.batch_writer import BatchWriter
        assert hasattr(BatchWriter, "flush")

    def test_batch_writer_has_adjust_batch_size(self):
        from shared_layer.database.batch_writer import BatchWriter
        assert hasattr(BatchWriter, "adjust_batch_size")

    def test_lazy_export_via_init(self):
        from shared_layer.database import BatchWriter
        assert BatchWriter is not None


class TestLocatorCacheModule:
    def test_import_locator_cache(self):
        from shared_layer.database.locator_cache import LocatorCache
        assert LocatorCache is not None

    def test_locator_cache_get_put(self):
        from shared_layer.database.locator_cache import LocatorCache
        cache = LocatorCache(max_size=10, ttl_seconds=60)
        cache.put("res-1", {"locator_type": "path", "locator_value": "/foo"})
        result = cache.get("res-1")
        assert result is not None
        assert result["locator_type"] == "path"

    def test_locator_cache_miss(self):
        from shared_layer.database.locator_cache import LocatorCache
        cache = LocatorCache(max_size=10, ttl_seconds=60)
        assert cache.get("missing") is None

    def test_locator_cache_invalidate(self):
        from shared_layer.database.locator_cache import LocatorCache
        cache = LocatorCache(max_size=10, ttl_seconds=60)
        cache.put("res-1", {"locator_type": "path"})
        cache.invalidate("res-1")
        assert cache.get("res-1") is None

    def test_locator_cache_stats(self):
        from shared_layer.database.locator_cache import LocatorCache
        cache = LocatorCache(max_size=10, ttl_seconds=60)
        cache.put("res-1", {"locator_type": "path"})
        cache.get("res-1")
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["size"] == 1

    def test_locator_cache_rejects_unrestricted_path(self):
        from shared_layer.database.locator_cache import LocatorCache
        cache = LocatorCache(max_size=10, ttl_seconds=60)
        cache.put("res-1", {"unrestricted_path": "/etc/passwd"})
        assert cache.get("res-1") is None

    def test_lazy_export_via_init(self):
        from shared_layer.database import LocatorCache, get_default_locator_cache
        assert LocatorCache is not None
        assert get_default_locator_cache() is not None


class TestPreparedQueryCatalogModule:
    def test_catalog_has_lookup_resource(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "lookup_resource" in CATALOG

    def test_catalog_has_claim_request(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "claim_request" in CATALOG

    def test_catalog_has_append_audit(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "append_audit" in CATALOG

    def test_catalog_has_update_index_state(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "update_index_state" in CATALOG

    def test_catalog_has_lookup_locator(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "lookup_locator" in CATALOG

    def test_catalog_has_fetch_relationships(self):
        from shared_layer.database.prepared_query_catalog import CATALOG
        assert "fetch_relationships" in CATALOG

    def test_get_query(self):
        from shared_layer.database.prepared_query_catalog import get_query
        sql = get_query("lookup_resource")
        assert "resource_id" in sql

    def test_list_queries(self):
        from shared_layer.database.prepared_query_catalog import list_queries
        queries = list_queries()
        assert "lookup_resource" in queries
        assert len(queries) >= 10

    def test_catalog_version(self):
        from shared_layer.database.prepared_query_catalog import catalog_version
        assert catalog_version() >= 1


class TestPerformanceBaselineModule:
    def test_import_record(self):
        from shared_layer.database.performance_baseline import record
        assert callable(record)

    def test_import_get_latest(self):
        from shared_layer.database.performance_baseline import get_latest
        assert callable(get_latest)

    def test_import_compare(self):
        from shared_layer.database.performance_baseline import compare
        assert callable(compare)

    def test_lazy_export_via_init(self):
        from shared_layer.database import record_baseline, get_latest_baseline, compare_baseline
        assert callable(record_baseline)
        assert callable(get_latest_baseline)
        assert callable(compare_baseline)


class TestQueryAllowlistPhaseE:
    def test_workload_pool_query(self):
        assert is_allowlisted("workload_pool.list")
        assert is_allowlisted("query_class.list")

    def test_query_fingerprint_query(self):
        assert is_allowlisted("query_fingerprint.hot")

    def test_transport_archive_query(self):
        assert is_allowlisted("tool_request_history.recent")

    def test_audit_archive_query(self):
        assert is_allowlisted("audit_event_history.recent")

    def test_partition_threshold_query(self):
        assert is_allowlisted("partition_threshold.list")

    def test_wal_checkpoint_query(self):
        assert is_allowlisted("wal_checkpoint.recent")

    def test_sqlite_class_query(self):
        assert is_allowlisted("sqlite_class.list")

    def test_reconcile_queue_query(self):
        assert is_allowlisted("reconcile_queue.pending")

    def test_performance_baseline_query(self):
        assert is_allowlisted("performance_baseline.latest")


class TestSchemaContractRegistryPhaseE:
    def test_expected_migration_count_is_85(self):
        assert EXPECTED_MIGRATION_COUNT == 85

    def test_contract_includes_workload_pool_config(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "workload_pool_config") in table_names

    def test_contract_includes_query_class(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "query_class") in table_names

    def test_contract_includes_query_fingerprint(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "query_fingerprint") in table_names

    def test_contract_includes_tool_request_history(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_transport", "tool_request_history") in table_names

    def test_contract_includes_event_history(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_audit", "event_history") in table_names

    def test_contract_includes_partition_threshold(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "partition_threshold") in table_names

    def test_contract_includes_wal_checkpoint_snapshot(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "wal_checkpoint_snapshot") in table_names

    def test_contract_includes_sqlite_database_class(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "sqlite_database_class") in table_names

    def test_contract_includes_reconcile_pending_queue(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "reconcile_pending_queue") in table_names

    def test_contract_includes_performance_baseline(self):
        contract = declared_contract()
        table_names = [(t.schema, t.table) for t in contract.tables]
        assert ("gptbridge_index", "performance_baseline") in table_names
