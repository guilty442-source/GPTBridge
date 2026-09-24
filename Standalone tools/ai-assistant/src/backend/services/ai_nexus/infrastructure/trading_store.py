"""Canonical trading store — ai-assistant business-data owner.

Tool-local authoritative store (manifest ``data_scope:
tool-database-only``). The legacy analytics database is archived and
sealed — this is a fresh schema for the rebuilt system. PostgreSQL
canonical migration is a governed release step tracked separately;
the repository interface is storage-agnostic so the swap is confined to
this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL,
    price REAL,
    quantity REAL,
    rationale TEXT,
    source TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    signal_id TEXT,
    strategy_id TEXT,
    instrument_id TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    price REAL,
    notional REAL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    approved INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    backend TEXT,
    payload TEXT NOT NULL,
    evaluated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    price REAL,
    status TEXT NOT NULL,
    strategy_id TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    rejection TEXT,
    simulated INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    account_id TEXT,
    instrument_id TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1,
    executed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS authorizations (
    grant_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    actor TEXT,
    payload TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_kv (
    domain TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (domain, key)
);
-- market-data mirror (authoritative business copy; engine journal is
-- the runtime mirror in investment-mobile)
CREATE TABLE IF NOT EXISTS market_quotes (
    instrument_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    source_id TEXT NOT NULL,
    currency TEXT,
    bid_price TEXT,
    ask_price TEXT,
    last_price TEXT,
    volume TEXT,
    source_timestamp TEXT,
    received_timestamp TEXT,
    market_session TEXT,
    data_status TEXT NOT NULL DEFAULT 'ok',
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_candles (
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_start TEXT NOT NULL,
    adjustment_type TEXT NOT NULL DEFAULT 'raw',
    market TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    turnover TEXT NOT NULL DEFAULT '0',
    currency TEXT,
    source_id TEXT NOT NULL,
    data_revision INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, adjustment_type)
);
CREATE TABLE IF NOT EXISTS market_source_status (
    source_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- fund domain mirror (engine journal mirrors → authoritative business copy)
CREATE TABLE IF NOT EXISTS fund_nav_mirror (
    fund_id TEXT NOT NULL,
    share_class_id TEXT NOT NULL,
    nav_date TEXT NOT NULL,
    nav_type TEXT NOT NULL DEFAULT 'published',
    nav TEXT NOT NULL,
    currency TEXT,
    source_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    data_status TEXT NOT NULL DEFAULT 'ok',
    payload TEXT NOT NULL,
    PRIMARY KEY (fund_id, share_class_id, nav_date, nav_type)
);
CREATE TABLE IF NOT EXISTS fund_transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    fund_id TEXT NOT NULL,
    share_class_id TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL,
    amount TEXT NOT NULL,
    units TEXT,
    confirmed_nav TEXT,
    currency TEXT,
    settlement_date TEXT,
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_distributions (
    distribution_id TEXT PRIMARY KEY,
    fund_id TEXT,
    share_class_id TEXT NOT NULL,
    ex_distribution_date TEXT NOT NULL,
    amount_per_unit TEXT NOT NULL,
    currency TEXT,
    distribution_source TEXT NOT NULL DEFAULT 'unconfirmed',
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    fund_id TEXT NOT NULL,
    share_class_id TEXT NOT NULL,
    account_id TEXT,
    recommendation_type TEXT NOT NULL,
    analysis_date TEXT,
    nav_date TEXT,
    model_id TEXT,
    model_version TEXT,
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fund_txn ON fund_transactions(account_id, fund_id, status);
CREATE INDEX IF NOT EXISTS idx_fund_nav ON fund_nav_mirror(fund_id, share_class_id, nav_date);
-- AI-intelligence mirror (advisory records — never executable)
CREATE TABLE IF NOT EXISTS ai_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    account_id TEXT,
    instrument_id TEXT NOT NULL,
    instrument_type TEXT,
    market TEXT,
    recommendation_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    model_id TEXT,
    model_version TEXT,
    data_quality TEXT,
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id TEXT PRIMARY KEY,
    task_kind TEXT,
    instrument_id TEXT,
    market TEXT,
    model_id TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
-- Strategy/backtest mirror (simulation records — never authoritative)
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    strategy_name TEXT,
    strategy_type TEXT,
    market TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_results (
    run_id TEXT PRIMARY KEY,
    strategy_id TEXT,
    market TEXT,
    total_return TEXT,
    max_drawdown TEXT,
    sharpe TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    simulated INTEGER NOT NULL DEFAULT 1,
    stale INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_recs ON ai_recommendations(instrument_id, status);
CREATE INDEX IF NOT EXISTS idx_bt_strategy ON backtest_results(strategy_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(type, at);
CREATE INDEX IF NOT EXISTS idx_candles_iid ON market_candles(instrument_id, timeframe, candle_start);
"""

# v1 → v2: align normalized columns with the unified trading contracts
# (instrument → instrument_id; fills → executions). Data is preserved
# via ALTER … RENAME — nothing is dropped.
_MIGRATE_V1_TO_V2 = """
ALTER TABLE signals RENAME COLUMN instrument TO instrument_id;
ALTER TABLE orders RENAME COLUMN instrument TO instrument_id;
ALTER TABLE fills RENAME TO executions;
ALTER TABLE executions RENAME COLUMN fill_id TO execution_id;
ALTER TABLE executions RENAME COLUMN instrument TO instrument_id;
ALTER TABLE executions ADD COLUMN account_id TEXT;
"""


class TradingStore:
    """Authoritative record store for the rebuilt investment domains."""

    def __init__(self, tool_root: Path) -> None:
        self._root = Path(tool_root)
        self._db_path = self._root / "runtime" / "data" / "trading_system.sqlite3"
        self._conn: sqlite3.Connection | None = None

    @property
    def database_path(self) -> Path:
        return self._db_path

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        version = 0
        if existing:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 0
        if version == 1:
            self._conn.executescript(_MIGRATE_V1_TO_V2)
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    # ------------------------------------------------------------------
    def record_signal(self, signal: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO signals(signal_id, market, instrument_id, side,"
            " confidence, price, quantity, rationale, source, payload, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(signal.get("signal_id") or f"sig-{int(time.time()*1000)}"),
                str(signal.get("market") or ""),
                str(signal.get("instrument_id") or signal.get("instrument") or ""),
                str(signal.get("side") or ""),
                signal.get("confidence"),
                signal.get("price"),
                signal.get("quantity"),
                str(signal.get("rationale") or ""),
                str(signal.get("source") or ""),
                json.dumps(signal, ensure_ascii=False),
                float(signal.get("created_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_proposal(self, proposal: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO proposals(proposal_id, signal_id, strategy_id,"
            " instrument_id, market, side, quantity, price, notional, payload,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(proposal.get("proposal_id") or f"prop-{int(time.time()*1000)}"),
                str(proposal.get("signal_id") or ""),
                str(proposal.get("strategy_id") or ""),
                str(proposal.get("instrument_id") or ""),
                str(proposal.get("market") or ""),
                str(proposal.get("side") or ""),
                proposal.get("quantity"),
                proposal.get("price"),
                proposal.get("notional"),
                json.dumps(proposal, ensure_ascii=False),
                float(proposal.get("created_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_decision(self, decision: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO risk_decisions(decision_id, proposal_id,"
            " approved, reasons, backend, payload, evaluated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                str(decision.get("decision_id") or f"risk-{int(time.time()*1000)}"),
                str(decision.get("proposal_id") or ""),
                1 if decision.get("approved") else 0,
                json.dumps(decision.get("reasons") or [], ensure_ascii=False),
                str(decision.get("backend") or "python"),
                json.dumps(decision, ensure_ascii=False),
                float(decision.get("evaluated_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_order(self, order: dict[str, Any]) -> None:
        proposal = order.get("proposal") or order
        self._db().execute(
            "INSERT OR REPLACE INTO orders(order_id, instrument_id, market, side,"
            " quantity, price, status, strategy_id, payload, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(order.get("order_id") or f"ord-{int(time.time()*1000)}"),
                str(proposal.get("instrument_id") or ""),
                str(proposal.get("market") or ""),
                str(proposal.get("side") or ""),
                proposal.get("quantity"),
                proposal.get("price"),
                str(order.get("status") or "created"),
                str(proposal.get("strategy_id") or ""),
                json.dumps(order, ensure_ascii=False),
                float(order.get("created_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_receipt(self, receipt: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO receipts(receipt_id, order_id, broker_order_id,"
            " status, rejection, simulated, payload, received_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                str(receipt.get("receipt_id") or f"rcpt-{int(time.time()*1000)}"),
                str(receipt.get("order_id") or ""),
                str(receipt.get("broker_order_id") or ""),
                str(receipt.get("status") or "submitted"),
                str(receipt.get("rejection") or ""),
                1 if receipt.get("simulated", True) else 0,
                json.dumps(receipt, ensure_ascii=False),
                float(receipt.get("received_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_execution(self, execution: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO executions(execution_id, order_id, account_id,"
            " instrument_id, market, side, quantity, price, simulated, executed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(execution.get("execution_id")
                    or execution.get("fill_id")
                    or f"exec-{int(time.time()*1000)}"),
                str(execution.get("order_id") or ""),
                str(execution.get("account_id") or ""),
                str(execution.get("instrument_id") or execution.get("instrument") or ""),
                str(execution.get("market") or ""),
                str(execution.get("side") or ""),
                float(execution.get("quantity") or 0.0),
                float(execution.get("price") or 0.0),
                1 if execution.get("simulated", True) else 0,
                float(execution.get("executed_at") or time.time()),
            ),
        )
        self._db().commit()

    # ------------------------------------------------------------------
    # market-data mirror
    def record_market_quote(self, quote: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO market_quotes(instrument_id, market,"
            " source_id, currency, bid_price, ask_price, last_price, volume,"
            " source_timestamp, received_timestamp, market_session,"
            " data_status, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(quote.get("instrument_id") or ""),
                str(quote.get("market") or ""),
                str(quote.get("source_id") or ""),
                str(quote.get("currency") or ""),
                quote.get("bid_price"), quote.get("ask_price"),
                quote.get("last_price"), str(quote.get("volume") or "0"),
                str(quote.get("source_timestamp") or ""),
                str(quote.get("received_timestamp") or ""),
                str(quote.get("market_session") or ""),
                str(quote.get("data_status") or "ok"),
                json.dumps(quote, ensure_ascii=False),
            ),
        )
        self._db().commit()

    def record_market_candle(self, candle: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO market_candles(instrument_id, timeframe,"
            " candle_start, adjustment_type, market, open, high, low, close,"
            " volume, turnover, currency, source_id, data_revision, payload)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(candle.get("instrument_id") or ""),
                str(candle.get("timeframe") or "1d"),
                str(candle.get("candle_start") or ""),
                str(candle.get("adjustment_type") or "raw"),
                str(candle.get("market") or ""),
                str(candle.get("open") or "0"), str(candle.get("high") or "0"),
                str(candle.get("low") or "0"), str(candle.get("close") or "0"),
                str(candle.get("volume") or "0"),
                str(candle.get("turnover") or "0"),
                str(candle.get("currency") or ""),
                str(candle.get("source_id") or ""),
                int(candle.get("data_revision") or 1),
                json.dumps(candle, ensure_ascii=False),
            ),
        )
        self._db().commit()

    def record_market_status(self, status: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO market_source_status(source_id, status,"
            " payload, updated_at) VALUES(?,?,?,?)",
            (
                str(status.get("source_id") or ""),
                str(status.get("connection_status") or ""),
                json.dumps(status, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def market_quote(self, instrument_id: str) -> dict[str, Any] | None:
        row = self._db().execute(
            "SELECT * FROM market_quotes WHERE instrument_id=?",
            (str(instrument_id),),
        ).fetchone()
        return dict(row) if row else None

    def market_candles(
        self,
        instrument_id: str,
        timeframe: str = "1d",
        limit: int = 500,
        adjustment_type: str = "raw",
    ) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM market_candles WHERE instrument_id=? AND timeframe=?"
            " AND adjustment_type=? ORDER BY candle_start LIMIT ?",
            (str(instrument_id), str(timeframe), str(adjustment_type), int(limit)),
        )

    def market_source_statuses(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM market_source_status")

    # ------------------------------------------------------------------
    # fund domain mirror
    def record_fund_nav(self, nav: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO fund_nav_mirror(fund_id, share_class_id,"
            " nav_date, nav_type, nav, currency, source_id, revision,"
            " data_status, payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(nav.get("fund_id") or ""),
                str(nav.get("share_class_id") or ""),
                str(nav.get("nav_date") or ""),
                str(nav.get("nav_type") or "published"),
                str(nav.get("nav") or "0"),
                str(nav.get("currency") or ""),
                str(nav.get("source_id") or ""),
                int(nav.get("revision") or 1),
                str(nav.get("data_status") or "ok"),
                json.dumps(nav, ensure_ascii=False),
            ),
        )
        self._db().commit()

    def record_fund_transaction(self, txn: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO fund_transactions(transaction_id,"
            " account_id, fund_id, share_class_id, transaction_type, status,"
            " amount, units, confirmed_nav, currency, settlement_date,"
            " payload, recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(txn.get("transaction_id") or ""),
                str(txn.get("account_id") or ""),
                str(txn.get("fund_id") or ""),
                str(txn.get("share_class_id") or ""),
                str(txn.get("transaction_type") or ""),
                str(txn.get("status") or ""),
                str(txn.get("amount") or "0"),
                str(txn.get("units") or "0"),
                str(txn.get("confirmed_nav") or ""),
                str(txn.get("currency") or ""),
                str(txn.get("settlement_date") or ""),
                json.dumps(txn, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def record_fund_distribution(self, dist: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO fund_distributions(distribution_id,"
            " fund_id, share_class_id, ex_distribution_date, amount_per_unit,"
            " currency, distribution_source, payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                str(dist.get("distribution_id") or ""),
                str(dist.get("fund_id") or ""),
                str(dist.get("share_class_id") or ""),
                str(dist.get("ex_distribution_date") or ""),
                str(dist.get("amount_per_unit") or "0"),
                str(dist.get("currency") or ""),
                str(dist.get("distribution_source") or "unconfirmed"),
                json.dumps(dist, ensure_ascii=False),
            ),
        )
        self._db().commit()

    def record_fund_recommendation(self, rec: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO fund_recommendations(recommendation_id,"
            " fund_id, share_class_id, account_id, recommendation_type,"
            " analysis_date, nav_date, model_id, model_version, payload,"
            " recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(rec.get("recommendation_id") or ""),
                str(rec.get("fund_id") or ""),
                str(rec.get("share_class_id") or ""),
                str(rec.get("account_id") or ""),
                str(rec.get("recommendation_type") or ""),
                str(rec.get("analysis_date") or ""),
                str(rec.get("nav_date") or ""),
                str(rec.get("model_id") or ""),
                str(rec.get("model_version") or ""),
                json.dumps(rec, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def fund_navs(self, fund_id: str, share_class_id: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
        if share_class_id:
            return self._rows(
                "SELECT * FROM fund_nav_mirror WHERE fund_id=?"
                " AND share_class_id=? ORDER BY nav_date LIMIT ?",
                (fund_id, share_class_id, int(limit)))
        return self._rows(
            "SELECT * FROM fund_nav_mirror WHERE fund_id=?"
            " ORDER BY nav_date LIMIT ?", (fund_id, int(limit)))

    def fund_transactions(self, account_id: str | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
        if account_id:
            return self._rows(
                "SELECT * FROM fund_transactions WHERE account_id=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (account_id, int(limit)))
        return self._rows(
            "SELECT * FROM fund_transactions ORDER BY recorded_at DESC"
            " LIMIT ?", (int(limit),))

    def fund_recommendations(self, fund_id: str | None = None,
                             limit: int = 100) -> list[dict[str, Any]]:
        if fund_id:
            return self._rows(
                "SELECT * FROM fund_recommendations WHERE fund_id=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (fund_id, int(limit)))
        return self._rows(
            "SELECT * FROM fund_recommendations ORDER BY recorded_at DESC"
            " LIMIT ?", (int(limit),))

    # ------------------------------------------------------------------
    # AI-intelligence mirror
    def record_ai_recommendation(self, rec: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO ai_recommendations(recommendation_id,"
            " account_id, instrument_id, instrument_type, market,"
            " recommendation_type, status, model_id, model_version,"
            " data_quality, payload, recorded_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(rec.get("recommendation_id") or ""),
                str(rec.get("account_id") or ""),
                str(rec.get("instrument_id") or ""),
                str(rec.get("instrument_type") or ""),
                str(rec.get("market") or ""),
                str(rec.get("recommendation_type") or ""),
                str(rec.get("status") or "CREATED"),
                str(rec.get("model_id") or ""),
                str(rec.get("model_version") or ""),
                str(rec.get("data_quality") or ""),
                json.dumps(rec, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def record_analysis_run(self, run: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO analysis_runs(run_id, task_kind,"
            " instrument_id, market, model_id, degraded, payload,"
            " recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(run.get("run_id") or ""),
                str(run.get("task_kind") or ""),
                str(run.get("instrument_id") or ""),
                str(run.get("market") or ""),
                str(run.get("model_id") or ""),
                1 if run.get("degraded") else 0,
                json.dumps(run, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def ai_recommendations(self, instrument_id: str | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        if instrument_id:
            return self._rows(
                "SELECT * FROM ai_recommendations WHERE instrument_id=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (instrument_id, int(limit)))
        return self._rows(
            "SELECT * FROM ai_recommendations ORDER BY recorded_at DESC"
            " LIMIT ?", (int(limit),))

    # ------------------------------------------------------------------
    # Strategy/backtest mirror (simulated artifacts — never authoritative)
    def record_strategy(self, row: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO strategies(strategy_id, strategy_name,"
            " strategy_type, market, version, status, payload, recorded_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_name") or ""),
                str(row.get("strategy_type") or ""),
                str(row.get("market") or ""),
                int(row.get("version") or 1),
                str(row.get("status") or "DRAFT"),
                json.dumps(row, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def record_backtest_result(self, result: dict[str, Any]) -> None:
        cfg = result.get("config") or {}
        self._db().execute(
            "INSERT OR REPLACE INTO backtest_results(run_id, strategy_id,"
            " market, total_return, max_drawdown, sharpe, trade_count,"
            " simulated, stale, payload, recorded_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(result.get("run_id") or ""),
                str(cfg.get("strategy_id") or ""),
                str(cfg.get("market") or ""),
                str(result.get("total_return") or ""),
                str(result.get("max_drawdown") or ""),
                str(result.get("sharpe") or ""),
                int(result.get("trade_count") or 0),
                1,
                1 if result.get("stale") else 0,
                json.dumps(result, ensure_ascii=False),
                time.time(),
            ),
        )
        self._db().commit()

    def backtest_results(self, strategy_id: str | None = None,
                         limit: int = 100) -> list[dict[str, Any]]:
        if strategy_id:
            return self._rows(
                "SELECT * FROM backtest_results WHERE strategy_id=?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (strategy_id, int(limit)))
        return self._rows(
            "SELECT * FROM backtest_results ORDER BY recorded_at DESC"
            " LIMIT ?", (int(limit),))

    def strategies(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM strategies ORDER BY recorded_at DESC")

    def record_authorization(self, grant: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO authorizations(grant_id, scope, granted_by,"
            " granted_at, expires_at, payload) VALUES(?,?,?,?,?,?)",
            (
                str(grant.get("grant_id") or f"grant-{int(time.time()*1000)}"),
                str(grant.get("scope") or "live-trading"),
                str(grant.get("granted_by") or ""),
                float(grant.get("granted_at") or time.time()),
                float(grant.get("expires_at") or 0.0),
                json.dumps(grant, ensure_ascii=False),
            ),
        )
        self._db().commit()

    def record_audit(self, event: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO audit_events(event_id, type, actor, payload, at)"
            " VALUES(?,?,?,?,?)",
            (
                str(event.get("event_id") or f"evt-{int(time.time()*1000)}"),
                str(event.get("type") or ""),
                str(event.get("actor") or ""),
                json.dumps(event, ensure_ascii=False),
                float(event.get("at") or time.time()),
            ),
        )
        self._db().commit()

    def record_report(self, kind: str, payload: dict[str, Any]) -> str:
        report_id = f"rpt-{int(time.time()*1000)}"
        self._db().execute(
            "INSERT INTO reports(report_id, kind, payload, created_at)"
            " VALUES(?,?,?,?)",
            (report_id, str(kind), json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self._db().commit()
        return report_id

    # ------------------------------------------------------------------
    def kv_set(self, domain: str, key: str, value: Any) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO domain_kv(domain, key, value, updated_at)"
            " VALUES(?,?,?,?)",
            (str(domain), str(key), json.dumps(value, ensure_ascii=False), time.time()),
        )
        self._db().commit()

    def kv_get(self, domain: str, key: str, default: Any = None) -> Any:
        row = self._db().execute(
            "SELECT value FROM domain_kv WHERE domain=? AND key=?",
            (str(domain), str(key)),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # ------------------------------------------------------------------
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cur = self._db().execute(sql, tuple(params))
        out = []
        for row in cur.fetchall():
            data = dict(row)
            payload = data.get("payload")
            if isinstance(payload, str):
                try:
                    data["payload"] = json.loads(payload)
                except json.JSONDecodeError:
                    pass
            out.append(data)
        return out

    def signals(self, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if market:
            return self._rows(
                "SELECT * FROM signals WHERE market=? ORDER BY created_at DESC LIMIT ?",
                (market, int(limit)),
            )
        return self._rows(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )

    def orders(self, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if market:
            return self._rows(
                "SELECT * FROM orders WHERE market=? ORDER BY created_at DESC LIMIT ?",
                (market, int(limit)),
            )
        return self._rows(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )

    def executions(self, market: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if market:
            return self._rows(
                "SELECT * FROM executions WHERE market=? ORDER BY executed_at DESC LIMIT ?",
                (market, int(limit)),
            )
        return self._rows(
            "SELECT * FROM executions ORDER BY executed_at DESC LIMIT ?", (int(limit),)
        )

    def authorizations(self, active_only: bool = True) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM authorizations ORDER BY granted_at DESC")
        if active_only:
            now = time.time()
            rows = [r for r in rows if float(r.get("expires_at") or 0) > now]
        return rows

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM audit_events ORDER BY at DESC LIMIT ?", (int(limit),)
        )

    # ------------------------------------------------------------------
    def positions(self, market: str | None = None) -> list[dict[str, Any]]:
        """Positions derived from the execution ledger (authoritative mirror)."""
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for execution in reversed(self.executions(market, limit=10000)):
            key = (str(execution["market"]), str(execution["instrument_id"]))
            entry = agg.setdefault(
                key,
                {
                    "market": key[0],
                    "instrument_id": key[1],
                    "quantity": 0.0,
                    "cost": 0.0,
                },
            )
            qty = float(execution["quantity"])
            if execution["side"] in ("sell", "redeem"):
                qty = -qty
            if qty > 0:
                entry["cost"] += qty * float(execution["price"])
            entry["quantity"] += qty
        out = []
        for entry in agg.values():
            if entry["quantity"] > 0:
                entry["average_cost"] = entry["cost"] / entry["quantity"]
                out.append(entry)
        return out
