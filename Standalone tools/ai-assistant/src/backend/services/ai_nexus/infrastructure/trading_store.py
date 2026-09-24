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

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL,
    price REAL,
    quantity REAL,
    rationale TEXT,
    source TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    price REAL,
    status TEXT NOT NULL,
    strategy_id TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
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
            "INSERT OR REPLACE INTO signals(signal_id, market, instrument, side,"
            " confidence, price, quantity, rationale, source, payload, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(signal.get("signal_id") or f"sig-{int(time.time()*1000)}"),
                str(signal.get("market") or ""),
                str(signal.get("instrument") or ""),
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

    def record_order(self, order: dict[str, Any]) -> None:
        intent = order.get("intent") or order
        self._db().execute(
            "INSERT OR REPLACE INTO orders(order_id, instrument, market, side,"
            " quantity, price, status, strategy_id, payload, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(order.get("order_id") or f"ord-{int(time.time()*1000)}"),
                str(intent.get("instrument") or ""),
                str(intent.get("market") or ""),
                str(intent.get("side") or ""),
                intent.get("quantity"),
                intent.get("price"),
                str(order.get("status") or "created"),
                str(intent.get("strategy_id") or ""),
                json.dumps(order, ensure_ascii=False),
                float(order.get("created_at") or time.time()),
            ),
        )
        self._db().commit()

    def record_fill(self, fill: dict[str, Any]) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO fills(fill_id, order_id, instrument, market,"
            " side, quantity, price, simulated, executed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(fill.get("fill_id") or f"fill-{int(time.time()*1000)}"),
                str(fill.get("order_id") or ""),
                str(fill.get("instrument") or ""),
                str(fill.get("market") or ""),
                str(fill.get("side") or ""),
                float(fill.get("quantity") or 0.0),
                float(fill.get("price") or 0.0),
                1 if fill.get("simulated", True) else 0,
                float(fill.get("executed_at") or time.time()),
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

    def fills(self, market: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if market:
            return self._rows(
                "SELECT * FROM fills WHERE market=? ORDER BY executed_at DESC LIMIT ?",
                (market, int(limit)),
            )
        return self._rows(
            "SELECT * FROM fills ORDER BY executed_at DESC LIMIT ?", (int(limit),)
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
        """Positions derived from the fill ledger (authoritative mirror)."""
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for fill in reversed(self.fills(market, limit=10000)):
            key = (str(fill["market"]), str(fill["instrument"]))
            entry = agg.setdefault(
                key,
                {
                    "market": key[0],
                    "instrument": key[1],
                    "quantity": 0.0,
                    "cost": 0.0,
                },
            )
            qty = float(fill["quantity"])
            if fill["side"] == "sell":
                qty = -qty
            if qty > 0:
                entry["cost"] += qty * float(fill["price"])
            entry["quantity"] += qty
        out = []
        for entry in agg.values():
            if entry["quantity"] > 0:
                entry["average_cost"] = entry["cost"] / entry["quantity"]
                out.append(entry)
        return out
