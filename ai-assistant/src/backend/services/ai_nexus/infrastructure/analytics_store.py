from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .privacy import (
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    privacy_status,
    protect_text,
    unprotect_text,
)
from .watch_repository import (
    _iter_migration_files,
    _validated_storage_path,
)
from ..domain.contract import INVESTMENT_ANALYTICS_SCHEMA_VERSION


SCHEMA_VERSION = INVESTMENT_ANALYTICS_SCHEMA_VERSION
DEFAULT_ALERT_COOLDOWN_MINUTES = 240
OPENING_BALANCE_PREFIX = "opening-balance:"
RECONCILIATION_PREFIX = "ledger-reconciliation:"
DEFAULT_BACKUP_RETENTION = 1
DEFAULT_AUTOMATIC_BACKUP_RETENTION = 1


class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""


def _portable_sqlite_image(data: bytes) -> bytes:
    """Make serialized WAL databases self-contained for memory deserialization."""
    if len(data) >= 20 and data.startswith(b"SQLite format 3\x00") and data[18:20] == b"\x02\x02":
        normalized = bytearray(data)
        normalized[18] = 1
        normalized[19] = 1
        return bytes(normalized)
    return data



from .analytics_helpers import *
from .analytics_finance import *
from .analytics_io import *
class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""



from .analytics_store_schema import InvestmentAnalyticsStoreSchema
from .analytics_store_queries import InvestmentAnalyticsStoreQueries

class InvestmentAnalyticsStore(InvestmentAnalyticsStoreSchema, InvestmentAnalyticsStoreQueries):

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = _validated_storage_path(
            Path(tool_root),
            label="AI assistant tool root",
            require_exists=True,
            expected_kind="directory",
        )
        self.legacy_runtime_root = _validated_storage_path(
            self.tool_root / "runtime",
            label="Legacy AI investment analytics runtime root",
            boundary=self.tool_root,
            expected_kind="directory",
        )
        self.runtime_root = _runtime_root(self.tool_root)
        self.database_path = self.runtime_root / "investment_analytics_v2.sqlite3"
        managed_storage = str(os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or "").strip()
        self._uses_external_managed_storage = bool(managed_storage)
        self.managed_storage_root = (
            Path(managed_storage).resolve()
            if managed_storage
            else self.runtime_root
        )
        self.backup_root = (
            self.managed_storage_root / "backups"
            if managed_storage
            else self.runtime_root / "investment_backups"
        )
        self.audit_root = (
            self.managed_storage_root / "audit" / "ai-assistant"
            if managed_storage
            else self.runtime_root / "audit"
        )
        self.audit_path = self.audit_root / "investment.jsonl"
        self.recovery_root = self.runtime_root / "recovery"
        self._database_lock = threading.RLock()
        self._database_key_id = ""
        self._batch_depth = 0
        self._pending_persist = False
        self._closed = False
        self._database_connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")
        self._durable_database_image: bytes | None = None
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.audit_root,
            label="AI investment analytics audit root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.audit_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.database_path,
            label="AI investment analytics database",
            boundary=self.runtime_root,
            expected_kind="file",
        )
        self._owner_lock = _RuntimeOwnerLock(
            self.runtime_root / ".investment-analytics-owner.lock",
            "AI investment analytics",
        )
        try:
            self._owner_lock.acquire()
            self._migrate_legacy_runtime()
            self._migrate_legacy_backups()
            self.prune_backups()
            self._load_database()
            self.initialize()
        except Exception:
            self._database_connection.close()
            self._closed = True
            self._owner_lock.release()
            raise


    def audit(self, action: str, details: dict[str, Any], *, severity: str = "info") -> None:
        audit_id = uuid.uuid4().hex
        occurred_at = utc_text()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(audit_id, occurred_at, action, severity, details_encrypted) VALUES(?, ?, ?, ?, ?)",
                (audit_id, occurred_at, action, severity, protect_text(_json(details))),
            )
        self._append_managed_audit_record(
            audit_id=audit_id,
            occurred_at=occurred_at,
            action=action,
            severity=severity,
            details=details,
        )


    def _append_managed_audit_record(
        self,
        *,
        audit_id: str,
        occurred_at: str,
        action: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        record = {
            "audit_id": audit_id,
            "occurred_at": occurred_at,
            "tool_id": "ai-assistant",
            "action": action,
            "severity": severity,
            "details_encrypted": protect_text(_json(details)),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())


    def list_audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT audit_id, occurred_at, action, severity, details_encrypted FROM audit_log ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["details"] = _decoded_json(
                unprotect_text(str(item.pop("details_encrypted", "") or "")), {}
            )
            output.append(item)
        return output


    def close(self) -> None:
        with self._database_lock:
            if self._closed:
                return
            try:
                self._persist_database()
            finally:
                try:
                    self._database_connection.close()
                finally:
                    self._closed = True
                    self._owner_lock.release()


    def __del__(self) -> None:
        """Release process resources without performing shutdown-time I/O.

        Every mutation is durably persisted by its operation. Explicit
        ``close()`` performs the final encrypted snapshot; garbage collection
        must not invoke DPAPI or filesystem writes while Python is finalizing.
        """

        if getattr(self, "_closed", True):
            return
        try:
            connection = getattr(self, "_database_connection", None)
            if connection is not None:
                connection.close()
        except BaseException:
            pass
        finally:
            self._closed = True
            try:
                owner_lock = getattr(self, "_owner_lock", None)
                if owner_lock is not None:
                    owner_lock.release()
            except BaseException:
                pass


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


    def record_analysis_snapshot(self, analysis: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        reports = analysis.get("holdings") if isinstance(analysis.get("holdings"), list) else []
        observed = parse_datetime(analysis.get("generated_at")) or utc_now()
        bars: list[dict[str, Any]] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
            price = number(quote.get("price"), -1)
            symbol = str(report.get("symbol") or "").upper()
            if not symbol or price <= 0:
                continue
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": quote.get("as_of") or utc_text(observed),
                    "close": price,
                    "currency": quote.get("currency") or report.get("currency"),
                    "provider": quote.get("provider") or "投資管家",
                    "verified": bool(report.get("trusted_quote")),
                }
            )
        self.add_price_bars(bars)
        latest = self.latest_prices()
        positions_by_symbol: dict[str, dict[str, Any]] = {}
        quoted_symbols: set[str] = set()
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").upper()
            quote = latest.get(symbol, {})
            quoted_price = number(quote.get("close"), -1)
            if quoted_price > 0:
                quoted_symbols.add(symbol)
            price = quoted_price if quoted_price > 0 else number(holding.get("average_cost"))
            quantity = number(holding.get("quantity"))
            average_cost = number(holding.get("average_cost"))
            if not symbol or price <= 0:
                continue
            position = positions_by_symbol.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": str(holding.get("market") or ""),
                    "asset_type": str(holding.get("asset_type") or ""),
                    "quantity": 0.0,
                    "price": price,
                    "market_value": 0.0,
                    "cost_value": 0.0,
                    "currency": str(
                        quote.get("currency") or holding.get("currency") or ""
                    ),
                },
            )
            position["quantity"] += quantity
            position["market_value"] += quantity * price
            position["cost_value"] += quantity * average_cost
        positions = list(positions_by_symbol.values())
        if not positions:
            return {"price_count": len(bars), "snapshot_saved": False}
        snapshot_id = uuid.uuid4().hex
        total_value = sum(item["market_value"] for item in positions)
        total_cost = sum(item["cost_value"] for item in positions)
        base_currency = str(self.get_setting("base_currency", "TWD") or "TWD")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO portfolio_snapshots VALUES(?, ?, ?, ?, ?, ?)",
                (snapshot_id, utc_text(observed), total_value, total_cost, base_currency, 0.0),
            )
            connection.executemany(
                """
                INSERT INTO snapshot_positions(
                    snapshot_id, symbol, market, asset_type, quantity, price,
                    market_value, cost_value, currency
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["symbol"],
                        item["market"],
                        item["asset_type"],
                        item["quantity"],
                        item["price"],
                        item["market_value"],
                        item["cost_value"],
                        item["currency"],
                    )
                    for item in positions
                ],
            )
        return {
            "price_count": len(bars),
            "snapshot_saved": True,
            "snapshot_id": snapshot_id,
            "quoted_position_count": len(quoted_symbols),
            "position_count": len(positions),
            "estimated_position_count": max(0, len(positions) - len(quoted_symbols)),
        }


    def current_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        latest = self.latest_prices()
        positions: list[dict[str, Any]] = []
        holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        use_twd_valuation = any(
            number(item.get(field)) > 0
            for item in holdings
            for field in ("principal_twd", "current_value_twd", "web_current_value_twd")
        )
        for holding in holdings:
            symbol = str(holding.get("symbol") or "").upper()
            quote = latest.get(symbol, {})
            price = number(quote.get("close"), number(holding.get("average_cost")))
            quantity = number(holding.get("quantity"))
            if quantity <= 0:
                continue
            native_cost = number(holding.get("average_cost")) * quantity
            native_value = price * quantity
            if use_twd_valuation:
                cost = number(holding.get("principal_twd"))
                if cost <= 0 and str(holding.get("currency") or "TWD").upper() == "TWD":
                    cost = number(holding.get("principal_amount"), native_cost)
                value = number(
                    holding.get("web_current_value_twd"),
                    number(holding.get("current_value_twd")),
                )
            else:
                cost = native_cost
                value = native_value
            positions.append(
                {
                    **holding,
                    "symbol": symbol,
                    "price": rounded(price),
                    "market_value": rounded(value, 2),
                    "cost_value": rounded(cost, 2),
                    "unrealized_pnl": rounded(value - cost, 2),
                    "unrealized_pnl_percent": rounded((value / cost - 1) * 100, 2) if cost > 0 else None,
                    "price_as_of": quote.get("observed_at"),
                }
            )
        total = sum(number(item.get("market_value")) for item in positions)
        for item in positions:
            item["weight_percent"] = rounded(number(item.get("market_value")) / total * 100, 2) if total > 0 else None
        return sorted(positions, key=lambda item: number(item.get("market_value")), reverse=True)


    def ledger_summary(self) -> dict[str, Any]:
        transactions = list(reversed(self.list_transactions(limit=5000)))
        lots: dict[str, list[list[float]]] = {}
        realized = 0.0
        dividends = 0.0
        fees = 0.0
        cashflows: list[dict[str, Any]] = []
        estimated_count = sum(1 for item in transactions if item.get("is_estimated"))
        for transaction in transactions:
            symbol = str(transaction.get("symbol") or "")
            side = str(transaction.get("side") or "")
            quantity = number(transaction.get("quantity"))
            price = number(transaction.get("price"))
            fee = number(transaction.get("fee"))
            tax = number(transaction.get("tax"))
            fees += fee + tax
            date = str(transaction.get("occurred_at") or "")
            if side == "BUY":
                lots.setdefault(symbol, []).append([quantity, price])
                cashflows.append({"date": date, "amount": -(quantity * price + fee + tax)})
            elif side == "SELL":
                remaining = quantity
                cost = 0.0
                for lot in lots.setdefault(symbol, []):
                    used = min(lot[0], remaining)
                    cost += used * lot[1]
                    lot[0] -= used
                    remaining -= used
                    if remaining <= 1e-10:
                        break
                lots[symbol] = [lot for lot in lots[symbol] if lot[0] > 1e-10]
                proceeds = quantity * price - fee - tax
                realized += proceeds - cost
                cashflows.append({"date": date, "amount": proceeds})
            elif side == "DIVIDEND":
                amount = price if quantity <= 0 else quantity * price
                dividends += amount
                cashflows.append({"date": date, "amount": amount})
            elif side == "CASH_IN":
                cashflows.append({"date": date, "amount": -price})
            elif side in {"CASH_OUT", "FEE"}:
                cashflows.append({"date": date, "amount": price})
        opening_ledger = self.get_setting("opening_ledger", {})
        confirmed_count = len(transactions) - estimated_count
        ledger_quality = (
            "empty"
            if not transactions
            else "estimated_opening"
            if estimated_count
            else "confirmed"
        )
        return {
            "transaction_count": len(transactions),
            "confirmed_transaction_count": confirmed_count,
            "estimated_transaction_count": estimated_count,
            "ledger_quality": ledger_quality,
            "opening_ledger": opening_ledger if isinstance(opening_ledger, dict) else {},
            "realized_pnl": rounded(realized, 2),
            "dividend_income": rounded(dividends, 2),
            "fees_and_taxes": rounded(fees, 2),
            "open_lot_count": sum(len(items) for items in lots.values()),
            "cashflows": cashflows[-500:],
        }


    def reconcile_ledger_holdings(self, state: dict[str, Any]) -> dict[str, Any]:
        holding_positions: dict[str, dict[str, Any]] = {}
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").strip().upper()
            quantity = number(holding.get("quantity"))
            if not symbol or quantity <= 0:
                continue
            row = holding_positions.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "quantity": 0.0,
                    "market": str(holding.get("market") or "").upper(),
                    "asset_type": str(holding.get("asset_type") or "").upper(),
                    "currency": str(holding.get("currency") or "TWD").upper(),
                    "principal_twd": 0.0,
                    "average_cost_total": 0.0,
                },
            )
            row["quantity"] += quantity
            row["principal_twd"] += number(holding.get("principal_twd"))
            row["average_cost_total"] += number(holding.get("average_cost")) * quantity

        ledger_positions: dict[str, float] = {}
        for transaction in reversed(self.list_transactions(5000)):
            symbol = str(transaction.get("symbol") or "").strip().upper()
            side = str(transaction.get("side") or "").upper()
            quantity = number(transaction.get("quantity"))
            if not symbol or side not in {"BUY", "SELL"}:
                continue
            ledger_positions[symbol] = ledger_positions.get(symbol, 0.0) + (
                quantity if side == "BUY" else -quantity
            )

        differences: list[dict[str, Any]] = []
        matched_count = 0
        all_symbols = sorted(set(holding_positions) | set(ledger_positions))
        for symbol in all_symbols:
            holding = holding_positions.get(symbol, {})
            holding_quantity = number(holding.get("quantity"))
            ledger_quantity = number(ledger_positions.get(symbol))
            delta = holding_quantity - ledger_quantity
            tolerance = max(0.000001, abs(holding_quantity) * 0.00001)
            matched = abs(delta) <= tolerance
            matched_count += int(matched)
            suggestion: dict[str, Any] | None = None
            if not matched:
                quantity = abs(delta)
                principal_twd = number(holding.get("principal_twd"))
                average_total = number(holding.get("average_cost_total"))
                price = (
                    principal_twd / holding_quantity
                    if holding_quantity > 0 and principal_twd > 0
                    else average_total / holding_quantity
                    if holding_quantity > 0 and average_total > 0
                    else 0.0
                )
                suggestion = {
                    "symbol": symbol,
                    "side": "BUY" if delta > 0 else "SELL",
                    "quantity": rounded(quantity, 8),
                    "price": rounded(price, 8),
                    "currency": "TWD" if principal_twd > 0 else str(holding.get("currency") or "TWD"),
                    "market": str(holding.get("market") or ""),
                    "asset_type": str(holding.get("asset_type") or ""),
                }
            differences.append(
                {
                    "symbol": symbol,
                    "holding_quantity": rounded(holding_quantity, 8),
                    "ledger_quantity": rounded(ledger_quantity, 8),
                    "difference_quantity": rounded(delta, 8),
                    "status": "matched" if matched else "difference",
                    "suggestion": suggestion,
                }
            )
        difference_rows = [item for item in differences if item["status"] == "difference"]
        return {
            "status": "ready" if all_symbols and not difference_rows else "differences" if all_symbols else "empty",
            "symbol_count": len(all_symbols),
            "matched_count": matched_count,
            "difference_count": len(difference_rows),
            "coverage_percent": rounded(matched_count / len(all_symbols) * 100, 2) if all_symbols else 0.0,
            "differences": difference_rows[:100],
            "generated_at": utc_text(),
        }


    def apply_ledger_reconciliation(
        self,
        state: dict[str, Any],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("ledger reconciliation requires confirmation")
        reconciliation = self.reconcile_ledger_holdings(state)
        applied = 0
        stamp = utc_text()
        for item in reconciliation.get("differences", []):
            suggestion = item.get("suggestion") if isinstance(item, dict) else None
            if not isinstance(suggestion, dict):
                continue
            if number(suggestion.get("quantity")) <= 0 or number(suggestion.get("price")) <= 0:
                continue
            identity = f"{stamp}|{suggestion.get('symbol')}|{suggestion.get('side')}|{suggestion.get('quantity')}"
            self.add_transaction(
                {
                    **suggestion,
                    "transaction_id": RECONCILIATION_PREFIX
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                    "occurred_at": stamp,
                    "note": "依目前持股與交易帳本差額建立的估算對帳調整；並非券商成交紀錄。",
                }
            )
            applied += 1
        result = self.reconcile_ledger_holdings(state)
        result["applied_count"] = applied
        self.audit("ledger_reconciliation_applied", result, severity="warning")
        return result


    def performance(self, state: dict[str, Any]) -> dict[str, Any]:
        positions = self.current_positions(state)
        current_value = sum(number(item.get("market_value")) for item in positions)
        current_cost = sum(number(item.get("cost_value")) for item in positions)
        ledger = self.ledger_summary()
        with self.connect() as connection:
            snapshots = [
                dict(row)
                for row in connection.execute(
                    "SELECT snapshot_id, observed_at, total_value, total_cost, base_currency, cash_value FROM portfolio_snapshots WHERE total_value > 0 ORDER BY observed_at LIMIT 2000"
                ).fetchall()
            ]
        values = [number(item.get("total_value")) for item in snapshots]
        daily_returns = _returns(values)
        twr = math.prod(1 + value for value in daily_returns) - 1 if daily_returns else None
        cashflows: list[tuple[datetime, float]] = []
        for item in ledger["cashflows"]:
            date = parse_datetime(item.get("date"))
            if date is not None:
                cashflows.append((date, number(item.get("amount"))))
        if current_value > 0:
            cashflows.append((utc_now(), current_value))
        irr = xirr(cashflows)
        attribution: dict[str, dict[str, float]] = {}
        for position in positions:
            currency = str(position.get("currency") or "UNKNOWN")
            bucket = attribution.setdefault(currency, {"market_value": 0.0, "cost_value": 0.0, "pnl": 0.0})
            bucket["market_value"] += number(position.get("market_value"))
            bucket["cost_value"] += number(position.get("cost_value"))
            bucket["pnl"] += number(position.get("unrealized_pnl"))
        return {
            "status": "ready" if positions else "empty",
            "methodology": "snapshot_twr_and_transaction_xirr",
            "current_value": rounded(current_value, 2),
            "current_cost": rounded(current_cost, 2),
            "unrealized_pnl": rounded(current_value - current_cost, 2),
            "unrealized_pnl_percent": rounded((current_value / current_cost - 1) * 100, 2) if current_cost > 0 else None,
            "realized_pnl": ledger["realized_pnl"],
            "dividend_income": ledger["dividend_income"],
            "fees_and_taxes": ledger["fees_and_taxes"],
            "twr_percent": rounded(twr * 100, 2) if twr is not None else None,
            "xirr_percent": rounded(irr * 100, 2) if irr is not None else None,
            "annualized_volatility_percent": rounded(statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) * 100, 2) if len(daily_returns) > 1 else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if values else None,
            "snapshot_count": len(snapshots),
            "attribution_by_currency": {key: {name: rounded(value, 2) for name, value in bucket.items()} for key, bucket in attribution.items()},
            "equity_curve": [
                {"date": item["observed_at"], "value": rounded(number(item["total_value"]), 2)}
                for item in snapshots[-365:]
            ],
            "positions": positions,
            "ledger": {key: value for key, value in ledger.items() if key != "cashflows"},
        }


    def risk(self, state: dict[str, Any], benchmark: str | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        raw_weights = {
            str(item.get("symbol") or ""): number(item.get("weight_percent")) / 100
            for item in positions
            if item.get("weight_percent") is not None
        }
        series = {
            symbol: self.price_series(symbol, 520)
            for symbol in raw_weights
        }
        returns_by_symbol: dict[str, dict[str, float]] = {}
        for symbol, bars in series.items():
            prices_by_date = {
                str(bar["observed_at"])[:10]: number(bar["close"])
                for bar in bars
                if number(bar.get("close")) > 0
            }
            ordered_dates = sorted(prices_by_date)
            returns_by_symbol[symbol] = {
                current: prices_by_date[current] / prices_by_date[previous] - 1
                for previous, current in zip(ordered_dates, ordered_dates[1:])
                if prices_by_date[previous] > 0
            }
        eligible_symbols = sorted(
            symbol for symbol, values in returns_by_symbol.items() if len(values) >= 20
        )
        common_dates = (
            sorted(
                set.intersection(
                    *(set(returns_by_symbol[symbol]) for symbol in eligible_symbols)
                )
            )
            if eligible_symbols
            else []
        )
        covered_weight = sum(raw_weights.get(symbol, 0.0) for symbol in eligible_symbols)
        weights = (
            {
                symbol: raw_weights.get(symbol, 0.0) / covered_weight
                for symbol in eligible_symbols
            }
            if covered_weight > 0
            else {}
        )
        aligned_returns = {
            symbol: [returns_by_symbol[symbol][date] for date in common_dates]
            for symbol in eligible_symbols
        }
        portfolio_returns_by_date = {
            date: sum(
                weights[symbol] * returns_by_symbol[symbol][date]
                for symbol in eligible_symbols
            )
            for date in common_dates
        }
        portfolio_returns = [
            portfolio_returns_by_date[date] for date in common_dates
        ]
        volatility = statistics.stdev(portfolio_returns) * math.sqrt(TRADING_DAYS) if len(portfolio_returns) > 1 else None
        var_cutoff = _percentile(portfolio_returns, 0.05)
        tail = [value for value in portfolio_returns if var_cutoff is not None and value <= var_cutoff]
        values = [1.0]
        for value in portfolio_returns:
            values.append(values[-1] * (1 + value))
        correlation: list[dict[str, Any]] = []
        symbols = eligible_symbols
        for left_index, left in enumerate(symbols):
            for right in symbols[left_index + 1 :]:
                coefficient = _correlation(
                    aligned_returns[left],
                    aligned_returns[right],
                )
                correlation.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": rounded(coefficient, 4),
                        "sample_count": len(common_dates),
                    }
                )
        benchmark_symbol = str(benchmark or self.get_setting("benchmark", DEFAULT_BENCHMARK) or DEFAULT_BENCHMARK).upper()
        benchmark_bars = self.price_series(benchmark_symbol, 520)
        benchmark_prices = {
            str(item["observed_at"])[:10]: number(item["close"])
            for item in benchmark_bars
            if number(item.get("close")) > 0
        }
        benchmark_dates = sorted(benchmark_prices)
        benchmark_returns_by_date = {
            current: benchmark_prices[current] / benchmark_prices[previous] - 1
            for previous, current in zip(benchmark_dates, benchmark_dates[1:])
            if benchmark_prices[previous] > 0
        }
        beta = None
        aligned_dates = sorted(set(portfolio_returns_by_date) & set(benchmark_returns_by_date))
        if len(aligned_dates) > 1:
            aligned_portfolio = [portfolio_returns_by_date[date] for date in aligned_dates]
            aligned_benchmark = [benchmark_returns_by_date[date] for date in aligned_dates]
            denominator = _variance(aligned_benchmark)
            beta = _covariance(aligned_portfolio, aligned_benchmark) / denominator if denominator > 0 else None
        exposures: dict[str, dict[str, float]] = {"market": {}, "currency": {}, "asset_type": {}}
        for item in positions:
            weight = number(item.get("weight_percent"))
            for dimension in exposures:
                key = str(item.get(dimension) or "UNKNOWN")
                exposures[dimension][key] = exposures[dimension].get(key, 0.0) + weight
        covariance = {
            (left, right): _covariance(aligned_returns[left], aligned_returns[right])
            for left in symbols
            for right in symbols
        }
        portfolio_variance = sum(
            weights[left] * weights[right] * covariance[(left, right)]
            for left in symbols
            for right in symbols
        )
        risk_contributions: list[dict[str, Any]] = []
        for symbol in symbols:
            covariance_with_portfolio = sum(
                covariance[(symbol, other)] * weights[other] for other in symbols
            )
            component_fraction = (
                weights[symbol] * covariance_with_portfolio / portfolio_variance
                if portfolio_variance > 0
                else None
            )
            risk_contributions.append(
                {
                    "symbol": symbol,
                    "weight_percent": rounded(weights[symbol] * 100, 2),
                    "portfolio_weight_percent": rounded(raw_weights.get(symbol, 0) * 100, 2),
                    "risk_contribution_percent": (
                        rounded(component_fraction * 100, 2)
                        if component_fraction is not None
                        else None
                    ),
                    "marginal_volatility_annualized_percent": (
                        rounded(
                            covariance_with_portfolio
                            / math.sqrt(portfolio_variance)
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if portfolio_variance > 0
                        else None
                    ),
                    "annualized_volatility_percent": (
                        rounded(
                            math.sqrt(_variance(aligned_returns[symbol]))
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if len(aligned_returns[symbol]) > 1
                        else None
                    ),
                }
            )
        total_market_value = sum(number(item.get("market_value")) for item in positions)
        analyzed_market_value = sum(
            number(item.get("market_value"))
            for item in positions
            if str(item.get("symbol") or "") in eligible_symbols
        )
        excluded_symbols = sorted(set(raw_weights) - set(eligible_symbols))
        return {
            "status": "ready" if len(portfolio_returns) >= 20 else "insufficient_history",
            "sample_count": len(portfolio_returns),
            "annualized_volatility_percent": rounded(volatility * 100, 2) if volatility is not None else None,
            "beta": rounded(beta, 3),
            "var_95_one_day_percent": rounded(-(var_cutoff or 0) * 100, 2) if var_cutoff is not None else None,
            "cvar_95_one_day_percent": rounded(-_mean(tail) * 100, 2) if tail else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if portfolio_returns else None,
            "correlations": sorted(correlation, key=lambda item: abs(number(item.get("correlation"))), reverse=True)[:100],
            "risk_contributions": sorted(risk_contributions, key=lambda item: number(item.get("risk_contribution_percent")), reverse=True),
            "exposures": {dimension: {key: rounded(value, 2) for key, value in values.items()} for dimension, values in exposures.items()},
            "benchmark": benchmark_symbol,
            "weight_methodology": "current_weights_proxy",
            "risk_contribution_methodology": "euler_marginal_contribution_from_covariance",
            "analysis_coverage": {
                "position_count": len(positions),
                "analyzed_position_count": len(eligible_symbols),
                "excluded_symbols": excluded_symbols,
                "market_value_percent": (
                    rounded(analyzed_market_value / total_market_value * 100, 2)
                    if total_market_value > 0
                    else None
                ),
                "requires_common_dates": True,
            },
            "limitations": [
                "Historical holdings are unavailable; current position weights are applied as an explicit proxy.",
                "Positions without at least 20 returns are excluded and remaining weights are renormalized.",
            ],
        }


    def stress_test(self, state: dict[str, Any], scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        configured = scenarios or [
            {"name": "大盤急跌", "market_shocks": {"*": -15}},
            {"name": "科技修正", "asset_type_shocks": {"STOCK": -10, "ETF": -7}},
            {"name": "美元升值", "currency_shocks": {"USD": 5, "TWD": -2}},
            {"name": "流動性壓力", "symbol_shocks": {}, "market_shocks": {"CRYPTO": -25, "*": -8}},
        ]
        total = sum(number(item.get("market_value")) for item in positions)
        results: list[dict[str, Any]] = []
        for scenario in configured:
            loss = 0.0
            impacts = []
            for position in positions:
                symbol = str(position.get("symbol") or "")
                market = str(position.get("market") or "")
                asset_type = str(position.get("asset_type") or "").upper()
                currency = str(position.get("currency") or "")
                symbol_shocks = scenario.get("symbol_shocks", {})
                market_shocks = scenario.get("market_shocks", {})
                asset_shocks = scenario.get("asset_type_shocks", {})
                currency_shocks = scenario.get("currency_shocks", {})
                shock = number(symbol_shocks.get(symbol), number(market_shocks.get(market), number(market_shocks.get("*"))))
                shock += number(asset_shocks.get(asset_type))
                shock += number(currency_shocks.get(currency))
                impact = number(position.get("market_value")) * shock / 100
                loss += impact
                impacts.append({"symbol": symbol, "shock_percent": rounded(shock, 2), "impact": rounded(impact, 2)})
            results.append(
                {
                    "name": str(scenario.get("name") or "自訂情境"),
                    "impact": rounded(loss, 2),
                    "impact_percent": rounded(loss / total * 100, 2) if total > 0 else None,
                    "projected_value": rounded(total + loss, 2),
                    "largest_impacts": sorted(impacts, key=lambda item: abs(number(item.get("impact"))), reverse=True)[:8],
                }
            )
        return {"status": "ready" if positions else "empty", "current_value": rounded(total, 2), "scenarios": results}


    def backtest(self, symbols: Sequence[str], strategy: str = "buy_and_hold", initial_capital: float = 1_000_000, fee_percent: float = 0.1425, slippage_percent: float = 0.05) -> dict[str, Any]:
        if strategy not in {"buy_and_hold", "equal_weight", "momentum"}:
            raise ValueError("unsupported backtest strategy")
        symbol_list = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        series = {
            symbol: {str(item["observed_at"])[:10]: number(item["close"]) for item in self.price_series(symbol, 2000)}
            for symbol in symbol_list
        }
        dates = sorted(set.intersection(*(set(values) for values in series.values()))) if series and all(series.values()) else []
        if len(dates) < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": len(dates),
                "message": "至少需要 30 個共同交易日。",
                "analysis_coverage": {
                    "requested_symbols": symbol_list,
                    "available_symbols": sorted(symbol for symbol, values in series.items() if values),
                    "common_date_count": len(dates),
                },
            }
        capital = max(1.0, initial_capital)
        target_weights = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
        cost_rate = (max(0.0, fee_percent) + max(0.0, slippage_percent)) / 100
        units = {
            symbol: (capital * target_weights[symbol])
            / (series[symbol][dates[0]] * (1 + cost_rate))
            for symbol in symbol_list
        }
        initial_transaction_cost = sum(
            units[symbol] * series[symbol][dates[0]] * cost_rate
            for symbol in symbol_list
        )
        cash = 0.0
        equity = sum(
            units[symbol] * series[symbol][dates[0]] for symbol in symbol_list
        )
        curve = [{"date": dates[0], "value": rounded(equity, 2)}]
        daily_returns: list[float] = []
        turnover_total = 0.0
        rebalance_cost = 0.0
        with self.connect() as connection:
            action_rows = connection.execute(
                """
                SELECT symbol, action_type, effective_at, ratio, cash_amount
                FROM corporate_actions
                WHERE status='approved' AND symbol IN ({})
                ORDER BY effective_at
                """.format(",".join("?" for _ in symbol_list)),
                symbol_list,
            ).fetchall()
        corporate_actions = [dict(row) for row in action_rows]
        applied_actions: list[dict[str, Any]] = []
        ignored_actions: list[dict[str, Any]] = []
        action_index = 0
        for index in range(1, len(dates)):
            previous_date, current_date = dates[index - 1], dates[index]
            while action_index < len(corporate_actions):
                action = corporate_actions[action_index]
                effective_date = str(action.get("effective_at") or "")[:10]
                if effective_date > current_date:
                    break
                action_index += 1
                if effective_date <= previous_date:
                    continue
                symbol = str(action.get("symbol") or "")
                action_type = str(action.get("action_type") or "")
                if symbol not in units:
                    continue
                if action_type == "split" and number(action.get("ratio")) > 0:
                    units[symbol] *= number(action.get("ratio"))
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "ratio": number(action.get("ratio")),
                        }
                    )
                elif action_type in {"dividend", "fund_distribution"} and number(action.get("cash_amount")) >= 0:
                    cash_amount = units[symbol] * number(action.get("cash_amount"))
                    cash += cash_amount
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "cash_amount": rounded(cash_amount, 4),
                        }
                    )
                else:
                    ignored_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "reason": "unsupported_in_backtest",
                        }
                    )
            previous_equity = equity
            current_values = {
                symbol: units[symbol] * series[symbol][current_date]
                for symbol in symbol_list
            }
            equity = cash + sum(current_values.values())
            if strategy in {"equal_weight", "momentum"} and index % 21 == 0:
                if strategy == "equal_weight":
                    targets = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
                else:
                    lookback = max(0, index - 60)
                    ranked = sorted(
                        symbol_list,
                        key=lambda symbol: series[symbol][previous_date] / series[symbol][dates[lookback]] - 1,
                        reverse=True,
                    )
                    selected = set(ranked[: max(1, math.ceil(len(ranked) / 2))])
                    targets = {symbol: (1 / len(selected) if symbol in selected else 0.0) for symbol in symbol_list}
                desired_before_cost = {
                    symbol: equity * targets[symbol] for symbol in symbol_list
                }
                traded_notional = sum(
                    abs(desired_before_cost[symbol] - current_values[symbol])
                    for symbol in symbol_list
                )
                transaction_cost = min(equity, traded_notional * cost_rate)
                post_cost_equity = max(0.0, equity - transaction_cost)
                turnover_total += (
                    traded_notional / (2 * equity) if equity > 0 else 0.0
                )
                rebalance_cost += transaction_cost
                units = {
                    symbol: (
                        post_cost_equity * targets[symbol]
                        / series[symbol][current_date]
                        if series[symbol][current_date] > 0
                        else 0.0
                    )
                    for symbol in symbol_list
                }
                cash = 0.0
                equity = post_cost_equity
                target_weights = targets
            daily_returns.append(
                equity / previous_equity - 1 if previous_equity > 0 else 0.0
            )
            curve.append({"date": current_date, "value": rounded(equity, 2)})
        values = [number(item["value"]) for item in curve]
        volatility = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) if len(daily_returns) > 1 else 0.0
        years = (len(dates) - 1) / TRADING_DAYS
        annual_return = (
            (equity / capital) ** (1 / years) - 1
            if years > 0 and equity > 0 and capital > 0
            else None
        )
        sharpe = (_mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)) if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0 else None
        ending_values = {
            symbol: units[symbol] * series[symbol][dates[-1]]
            for symbol in symbol_list
        }
        ending_total = cash + sum(ending_values.values())
        return {
            "ok": True,
            "status": "ready",
            "strategy": strategy,
            "symbols": symbol_list,
            "sample_count": len(dates),
            "initial_capital": rounded(capital, 2),
            "ending_value": rounded(equity, 2),
            "total_return_percent": rounded((equity / capital - 1) * 100, 2),
            "annualized_return_percent": rounded((annual_return or 0) * 100, 2) if annual_return is not None else None,
            "annualized_volatility_percent": rounded(volatility * 100, 2),
            "sharpe_ratio": rounded(sharpe, 3),
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2),
            "turnover_percent": rounded(turnover_total * 100, 2),
            "ending_weights": {
                symbol: (
                    rounded(ending_values[symbol] / ending_total * 100, 4)
                    if ending_total > 0
                    else None
                )
                for symbol in symbol_list
            },
            "cost_assumptions": {
                "fee_percent": fee_percent,
                "slippage_percent": slippage_percent,
                "initial_transaction_cost": rounded(initial_transaction_cost, 2),
                "rebalance_transaction_cost": rounded(rebalance_cost, 2),
                "total_transaction_cost": rounded(initial_transaction_cost + rebalance_cost, 2),
                "initial_purchase_included": True,
            },
            "methodology": "unit_based_holdings_with_natural_weight_drift",
            "lookahead_protection": "signals use only prices available before each rebalance",
            "corporate_actions": {
                "policy": "approved actions only",
                "applied": applied_actions,
                "ignored": ignored_actions,
            },
            "analysis_coverage": {
                "requested_symbols": symbol_list,
                "available_symbols": symbol_list,
                "common_date_count": len(dates),
                "start_date": dates[0],
                "end_date": dates[-1],
            },
            "assumptions": [
                "Stored close prices are treated as unadjusted prices.",
                "Approved splits adjust units and approved cash distributions enter cash.",
                "Taxes, FX conversion, delistings, survivorship bias and unapproved corporate actions are not modeled.",
            ],
            "equity_curve": curve[-800:],
        }


    def rebalance(self, state: dict[str, Any], targets: dict[str, float] | None = None, max_position_percent: float = 35, cash_reserve_percent: float = 5, min_trade_value: float = 1000, fee_percent: float = 0.1425) -> dict[str, Any]:
        positions = self.current_positions(state)
        total = sum(number(item.get("market_value")) for item in positions)
        if total <= 0:
            return {"ok": False, "status": "empty", "orders": []}
        symbols = [str(item.get("symbol") or "") for item in positions]
        raw_targets = {str(key).upper(): max(0.0, number(value)) for key, value in (targets or {}).items()}
        if not raw_targets:
            raw_targets = {symbol: 100 / len(symbols) for symbol in symbols}
        investable_percent = max(0.0, 100 - cash_reserve_percent)
        position_cap = max(0.0, min(100.0, max_position_percent))
        normalized = {symbol: 0.0 for symbol in symbols}
        active = {symbol for symbol in symbols if raw_targets.get(symbol, 0.0) > 0}
        remaining = investable_percent
        while active and remaining > 1e-9:
            active_total = sum(raw_targets[symbol] for symbol in active)
            if active_total <= 0:
                break
            capped_symbols = {
                symbol
                for symbol in active
                if remaining * raw_targets[symbol] / active_total > position_cap
            }
            if not capped_symbols:
                for symbol in active:
                    normalized[symbol] += remaining * raw_targets[symbol] / active_total
                remaining = 0.0
                break
            for symbol in capped_symbols:
                allocation = min(position_cap - normalized[symbol], remaining)
                normalized[symbol] += max(0.0, allocation)
                remaining -= max(0.0, allocation)
                active.remove(symbol)
        actual_invested_percent = sum(normalized.values())
        orders = []
        estimated_fees = 0.0
        for position in positions:
            symbol = str(position.get("symbol") or "")
            current_value = number(position.get("market_value"))
            target_value = total * normalized.get(symbol, 0) / 100
            difference = target_value - current_value
            if abs(difference) < max(0.0, min_trade_value):
                continue
            price = number(position.get("price"))
            quantity = abs(difference) / price if price > 0 else 0
            fee = abs(difference) * max(0.0, fee_percent) / 100
            estimated_fees += fee
            orders.append(
                {
                    "symbol": symbol,
                    "side": "BUY" if difference > 0 else "SELL",
                    "quantity": rounded(quantity, 4),
                    "estimated_value": rounded(abs(difference), 2),
                    "estimated_fee": rounded(fee, 2),
                    "current_weight_percent": position.get("weight_percent"),
                    "target_weight_percent": rounded(normalized.get(symbol, 0), 2),
                    "price": rounded(price, 4),
                }
            )
        return {
            "ok": True,
            "status": "draft",
            "execution_policy": "simulation_only_human_approval_required",
            "portfolio_value": rounded(total, 2),
            "cash_reserve_percent": rounded(max(0.0, 100 - actual_invested_percent), 2),
            "requested_cash_reserve_percent": cash_reserve_percent,
            "max_position_percent": position_cap,
            "estimated_fees": rounded(estimated_fees, 2),
            "orders": sorted(orders, key=lambda item: number(item.get("estimated_value")), reverse=True),
            "before_risk": self.risk(state),
        }


    def calibration(self) -> dict[str, Any]:
        all_decisions = self.decisions(2000)
        evaluated = [
            item
            for item in all_decisions
            if item.get("outcome", {}).get("evaluable") is True
            and bool(item.get("eligible_for_calibration"))
        ]
        scored: list[float] = []
        correct = 0
        bucket_values: dict[str, list[tuple[float, float]]] = {
            "low": [],
            "medium": [],
            "high": [],
        }
        for item in evaluated:
            outcome = (
                1.0 if item.get("outcome", {}).get("success") is True else 0.0
            )
            probability = normalized_probability(item.get("confidence"), 0.5)
            correct += int(outcome == 1.0)
            scored.append((probability - outcome) ** 2)
            bucket = "high" if probability >= 0.75 else "medium" if probability >= 0.55 else "low"
            bucket_values[bucket].append((probability, outcome))
        buckets = [
            {
                "key": key,
                "label": {"low": "低信心", "medium": "中信心", "high": "高信心"}[key],
                "count": len(values),
                "average_confidence": rounded(_mean([value[0] for value in values]) * 100, 2) if values else None,
                "accuracy_percent": rounded(_mean([value[1] for value in values]) * 100, 2) if values else None,
            }
            for key, values in bucket_values.items()
        ]
        average_confidence = _mean(
            [normalized_probability(item.get("confidence"), 0.5) for item in evaluated]
        ) if evaluated else 0.0
        accuracy = correct / len(evaluated) if evaluated else 0.0
        reliability_gap = average_confidence - accuracy
        confidence_multiplier = max(0.65, min(1.05, 1.0 - max(0.0, reliability_gap)))
        slices: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for item in evaluated:
            probability = normalized_probability(item.get("confidence"), 0.5)
            outcome = 1.0 if item.get("outcome", {}).get("success") is True else 0.0
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            dimensions = {
                "direction": str(item.get("prediction_direction") or "unknown"),
                "market": str(evidence.get("market") or "UNKNOWN"),
                "asset_type": str(evidence.get("asset_type") or "UNKNOWN"),
            }
            for dimension, key in dimensions.items():
                grouped.setdefault((dimension, key), []).append((probability, outcome))
        for (dimension, key), values in sorted(grouped.items()):
            slices.append(
                {
                    "dimension": dimension,
                    "key": key,
                    "evaluated_count": len(values),
                    "brier_score": rounded(
                        _mean([(probability - outcome) ** 2 for probability, outcome in values]),
                        4,
                    ),
                    "accuracy_percent": rounded(
                        _mean([outcome for _probability, outcome in values]) * 100,
                        2,
                    ),
                    "average_confidence_percent": rounded(
                        _mean([probability for probability, _outcome in values]) * 100,
                        2,
                    ),
                }
            )
        sample_version = (
            hashlib.sha256(
                _json(
                    [
                        {
                            "decision_id": item.get("decision_id"),
                            "evaluated_at": item.get("outcome", {}).get("evaluated_at"),
                            "direction": item.get("prediction_direction"),
                            "success": item.get("outcome", {}).get("success"),
                        }
                        for item in evaluated
                    ]
                ).encode("utf-8")
            ).hexdigest()[:24]
            if evaluated
            else ""
        )
        return {
            "status": "ready" if scored else "collecting",
            "evaluated_count": len(scored),
            "pending_count": sum(
                1
                for item in all_decisions
                if bool(item.get("eligible_for_calibration")) and not item.get("outcome")
            ),
            "not_calibrated_count": sum(
                1
                for item in all_decisions
                if not bool(item.get("eligible_for_calibration"))
            ),
            "brier_score": rounded(_mean(scored), 4) if scored else None,
            "accuracy_percent": rounded(accuracy * 100, 2) if scored else None,
            "average_confidence_percent": rounded(average_confidence * 100, 2) if scored else None,
            "reliability_gap_percent": rounded(reliability_gap * 100, 2) if scored else None,
            "confidence_multiplier": rounded(confidence_multiplier, 4),
            "confidence_buckets": buckets,
            "sample_version": sample_version,
            "slices": slices,
            "calibration_label": "穩定" if scored and _mean(scored) <= 0.2 else "需校準" if scored else "累積結果中",
        }


    def analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        with self.batch_updates():
            return self._analytics_snapshot(state)


    def _analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        performance = self.performance(state)
        ledger = performance.get("ledger", {})
        risk = self.risk(state)
        self.update_decision_outcomes()
        triggered = self.evaluate_alerts(state, risk)
        with self.connect() as connection:
            counts = {
                table: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
                        if table == "transactions"
                        else f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "transactions",
                    "prices",
                    "portfolio_snapshots",
                    "market_events",
                    "alert_rules",
                    "alert_events",
                    "decisions",
                )
            }
            tombstone_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transactions WHERE deleted_at <> ''"
                ).fetchone()[0]
            )
        return {
            "version": "1.0.0",
            "generated_at": utc_text(),
            "database_path": str(self.database_path),
            "data_health": {
                "schema_version": SCHEMA_VERSION,
                "counts": counts,
                "transaction_tombstone_count": tombstone_count,
                "history_ready": counts["prices"] >= max(30, len(state.get("holdings", [])) * 30),
                "ledger_ready": counts["transactions"] > 0,
                "ledger_quality": ledger.get("ledger_quality", "empty"),
                "warnings": [
                    message
                    for condition, message in (
                        (counts["transactions"] == 0, "尚未建立交易帳本，XIRR 與已實現損益可能不完整。"),
                        (risk.get("status") != "ready", "歷史行情不足，專業風險指標暫不完整。"),
                    )
                    if condition
                ],
            },
            "privacy": privacy_status(),
            "performance": performance,
            "risk": risk,
            "stress": self.stress_test(state),
            "ledger": {
                "transactions": self.list_transactions(100),
                **ledger,
                "reconciliation": self.reconcile_ledger_holdings(state),
            },
            "events": self.list_events(100),
            "alerts": {
                "rules": self.list_alert_rules(),
                "events": self.list_alert_events(100),
                "new_count": len(triggered),
                "unacknowledged_count": sum(1 for item in self.list_alert_events(500) if not item.get("acknowledged_at")),
            },
            "decisions": self.decisions(100),
            "calibration": self.calibration(),
        }


POSITIVE_WORDS = {"beat", "growth", "raise", "upgrade", "profit", "surge", "record", "成長", "上修", "獲利", "創高", "優於"}
NEGATIVE_WORDS = {"miss", "cut", "downgrade", "loss", "fall", "risk", "fraud", "下修", "虧損", "衰退", "風險", "裁員"}



__all__ = ['InvestmentAnalyticsUpgradeRequired', 'SCHEMA_VERSION', 'DEFAULT_ALERT_COOLDOWN_MINUTES', 'OPENING_BALANCE_PREFIX', 'RECONCILIATION_PREFIX', 'DEFAULT_BACKUP_RETENTION', 'DEFAULT_AUTOMATIC_BACKUP_RETENTION', 'InvestmentAnalyticsStore']
