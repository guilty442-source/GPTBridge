from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .analytics_common import (
    SCHEMA_VERSION,
    utc_text,
)


class SchemaMixin:
    """Schema initialization, migration, and data clearing methods."""

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = MEMORY")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT '',
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0,
                    note_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT '',
                    delete_reason_encrypted TEXT NOT NULL DEFAULT '',
                    delete_audit_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_transactions_time ON transactions(occurred_at);
                CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol, occurred_at);
                CREATE TABLE IF NOT EXISTS prices (
                    symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL NOT NULL,
                    volume REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(symbol, observed_at, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_prices_symbol_time ON prices(symbol, observed_at);
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    total_value REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    base_currency TEXT NOT NULL DEFAULT '',
                    cash_value REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshot_positions (
                    snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    market_value REAL NOT NULL,
                    cost_value REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(snapshot_id, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_time ON portfolio_snapshots(observed_at);
                CREATE TABLE IF NOT EXISTS market_events (
                    event_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    source_url_encrypted TEXT NOT NULL DEFAULT '',
                    sentiment REAL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    details_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON market_events(scheduled_at);
                CREATE TABLE IF NOT EXISTS alert_rules (
                    rule_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    operator TEXT NOT NULL DEFAULT '>=',
                    threshold REAL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    cooldown_minutes INTEGER NOT NULL DEFAULT 240,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_events (
                    alert_event_id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL REFERENCES alert_rules(rule_id) ON DELETE CASCADE,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    triggered_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    value REAL,
                    acknowledged_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_time ON alert_events(triggered_at);
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    confidence REAL,
                    score REAL,
                    risk_level TEXT NOT NULL DEFAULT '',
                    reference_price REAL,
                    evidence_encrypted TEXT NOT NULL DEFAULT '',
                    snapshot_encrypted TEXT NOT NULL DEFAULT '',
                    user_status TEXT NOT NULL DEFAULT 'pending',
                    outcome_due_at TEXT NOT NULL,
                    outcome_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_due ON decisions(outcome_due_at);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fx_rates (
                    base_currency TEXT NOT NULL,
                    quote_currency TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    rate REAL NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(base_currency, quote_currency, observed_at, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_fx_time
                    ON fx_rates(base_currency, quote_currency, observed_at);
                CREATE TABLE IF NOT EXISTS broker_imports (
                    import_id TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    broker TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    difference_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'review',
                    summary_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS broker_import_rows (
                    row_id TEXT PRIMARY KEY,
                    import_id TEXT NOT NULL REFERENCES broker_imports(import_id) ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    quantity REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT '',
                    match_status TEXT NOT NULL DEFAULT 'unmatched',
                    matched_transaction_id TEXT NOT NULL DEFAULT '',
                    raw_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_broker_rows_import
                    ON broker_import_rows(import_id, match_status);
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    action_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    ratio REAL,
                    cash_amount REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    old_symbol TEXT NOT NULL DEFAULT '',
                    new_symbol TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    details_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_corporate_actions_time
                    ON corporate_actions(symbol, effective_at);
                CREATE TABLE IF NOT EXISTS data_quality_issues (
                    issue_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'warning',
                    title TEXT NOT NULL,
                    detail_encrypted TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    resolved_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    run_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    detail_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_scheduler_runs_time
                    ON scheduler_runs(started_at);
                CREATE TABLE IF NOT EXISTS import_operations (
                    operation_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL UNIQUE,
                    import_fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payload_encrypted TEXT NOT NULL,
                    result_encrypted TEXT NOT NULL DEFAULT '',
                    error_encrypted TEXT NOT NULL DEFAULT '',
                    history_encrypted TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_import_operations_status
                    ON import_operations(status, updated_at);
                CREATE TABLE IF NOT EXISTS notification_channels (
                    channel_id TEXT PRIMARY KEY,
                    channel_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    config_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    notification_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    channel_id TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    body_encrypted TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_at TEXT NOT NULL DEFAULT '',
                    error_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_status
                    ON notification_outbox(status, created_at);
                CREATE TABLE IF NOT EXISTS model_versions (
                    model_version_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    config_encrypted TEXT NOT NULL DEFAULT '',
                    UNIQUE(model_name, version, prompt_hash)
                );
                CREATE TABLE IF NOT EXISTS model_governance_runs (
                    governance_run_id TEXT PRIMARY KEY,
                    model_version_id TEXT NOT NULL REFERENCES model_versions(model_version_id),
                    analysis_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    data_sources_encrypted TEXT NOT NULL DEFAULT '',
                    metrics_encrypted TEXT NOT NULL DEFAULT '',
                    decision_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_model_governance_time
                    ON model_governance_runs(created_at);
                CREATE TABLE IF NOT EXISTS audit_log (
                    audit_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    details_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(occurred_at);
                """
            )
            self._migrate_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(SCHEMA_VERSION), utc_text()),
            )
        self.ensure_default_alerts()

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        transaction_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
        }
        transaction_additions = {
            "deleted_at": "TEXT NOT NULL DEFAULT ''",
            "delete_reason_encrypted": "TEXT NOT NULL DEFAULT ''",
            "delete_audit_id": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in transaction_additions.items():
            if column not in transaction_columns:
                connection.execute(
                    f"ALTER TABLE transactions ADD COLUMN {column} {declaration}"
                )

        decision_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
        }
        additions = {
            "prediction_direction": "TEXT NOT NULL DEFAULT 'abstain'",
            "horizon_days": "INTEGER NOT NULL DEFAULT 30",
            "return_threshold_percent": "REAL NOT NULL DEFAULT 0",
            "eligible_for_calibration": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in additions.items():
            if column not in decision_columns:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column} {declaration}")
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key='decision_confidence_normalized_v1'"
        ).fetchone()
        if marker is None:
            # v3 accepted both scales, then divided all inputs by 100. Values at or
            # below 0.01 are therefore recognizable legacy 0..1 inputs.
            connection.execute(
                """
                UPDATE decisions
                SET confidence = CASE
                    WHEN confidence IS NULL THEN NULL
                    WHEN confidence < 0 THEN 0
                    WHEN confidence > 100 THEN 1
                    WHEN confidence > 1 THEN confidence / 100.0
                    WHEN confidence > 0 AND confidence <= 0.01 THEN confidence * 100.0
                    ELSE confidence
                END
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)",
                ("decision_confidence_normalized_v1", "complete", utc_text()),
            )

    def clear_data(self, *, permanent: bool = False) -> dict[str, Any]:
        safety = (
            {"ok": True, "created": False, "permanent": True, "path": ""}
            if permanent
            else self.backup_database("before-clear")
        )
        with self.connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT IN ('metadata', 'sqlite_sequence')"
                ).fetchall()
            ]
            table_names = set(tables)
            child_to_parents: dict[str, set[str]] = {table: set() for table in tables}
            parent_indegree: dict[str, int] = {table: 0 for table in tables}
            for table in tables:
                if not table.replace("_", "").isalnum():
                    raise RuntimeError("invalid analytics table name")
                parents = {
                    str(row[2])
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                    if str(row[2]) in table_names and str(row[2]) != table
                }
                child_to_parents[table] = parents
                for parent in parents:
                    parent_indegree[parent] += 1
            ready = sorted(
                table for table, indegree in parent_indegree.items() if indegree == 0
            )
            deletion_order: list[str] = []
            while ready:
                table = ready.pop(0)
                deletion_order.append(table)
                for parent in sorted(child_to_parents[table]):
                    parent_indegree[parent] -= 1
                    if parent_indegree[parent] == 0:
                        ready.append(parent)
                        ready.sort()
            deletion_order.extend(
                sorted(table_names.difference(deletion_order))
            )
            for table in deletion_order:
                connection.execute(f"DELETE FROM {table}")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'sqlite_sequence'"
            ).fetchone():
                connection.execute("DELETE FROM sqlite_sequence")
        if permanent:
            roots = [self.backup_root, self.recovery_root]
            if not self._uses_external_managed_storage:
                roots.append(self.audit_root)
            for root in roots:
                if not root.is_dir():
                    continue
                for current_root, directory_names, file_names in os.walk(
                    root,
                    topdown=False,
                    followlinks=False,
                ):
                    current_path = Path(current_root)
                    for name in file_names:
                        (current_path / name).unlink()
                    for name in directory_names:
                        child = current_path / name
                        if child.is_symlink():
                            child.unlink()
                        else:
                            child.rmdir()
            with self._database_lock:
                self._database_connection.execute("VACUUM")
                self._persist_database(rotate_key=True)
        return safety
