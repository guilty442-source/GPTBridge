-- 145_baseline_reanchor.sql — A502 baseline re-anchor (governed).
-- Executor: SQL_MIGRATION_EXECUTOR only (sql_migration_executor_contract).
--
-- Purpose: the declared authority chain terminates at 049_roll_forward,
-- but shared-layer/migrations 050..144 were applied to live PostgreSQL
-- without registry entries and without receipts (sql_migration_receipt_
-- registry is empty).  The live schema hash is therefore unverifiable
-- against the declared terminal hash, which is exactly the state A502
-- keeps fail-closed.
--
-- This migration is data-only: it stamps the re-anchor marker so the
-- canonical schema surface is unchanged by the migration itself, and
-- the receipt's observed_target_hash IS the live canonical hash
-- (SQL_DDL_CANONICALIZATION_V1).  The governed runner inserts the
-- sql_migration_registry / sql_migration_authority_registry rows and
-- the receipt row in the same transaction.

INSERT INTO gptbridge_codex.metadata (key, value)
VALUES ('a502_baseline_reanchor', 'pending-executor-stamp')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
