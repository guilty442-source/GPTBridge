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
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(type, at);
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
