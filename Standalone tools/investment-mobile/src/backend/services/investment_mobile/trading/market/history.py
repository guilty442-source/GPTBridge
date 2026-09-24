"""HistoricalMarketDataService — incremental candle store + sync.

Local SQLite cache (private runtime state — the authoritative copy is
mirrored into PostgreSQL ``gptbridge_trading.market_candle`` by the
business layer through the governed channel).

Guarantees:
- Incremental: per (source, instrument, timeframe) sync cursor —
  downloads never restart from zero.
- Dedup: unique (instrument_id, timeframe, candle_start,
  adjustment_type); a correction lands as a higher ``data_revision``
  and supersedes, never duplicates.
- Integrity: gap detection against the trading calendar; a disconnected
  source never triggers deletion — gaps are flagged, not filled with
  fabricated data.
- Replay: deterministic ordering for backtests.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .calendar import TradingCalendar
from .contracts import MarketCandle, utcnow
from .sources import MarketDataSource

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candle (
    instrument_id   TEXT NOT NULL,
    market          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    candle_start    TEXT NOT NULL,   -- ISO-8601 UTC, timezone-aware
    candle_end      TEXT NOT NULL,
    open            TEXT NOT NULL,   -- Decimal-as-text, exact
    high            TEXT NOT NULL,
    low             TEXT NOT NULL,
    close           TEXT NOT NULL,
    volume          TEXT NOT NULL,
    turnover        TEXT NOT NULL DEFAULT '0',
    currency        TEXT NOT NULL DEFAULT '',
    source_id       TEXT NOT NULL,
    data_revision   INTEGER NOT NULL DEFAULT 1,
    adjustment_type TEXT NOT NULL DEFAULT 'raw',
    received_at     TEXT NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, candle_start, adjustment_type)
);
CREATE INDEX IF NOT EXISTS candle_market_time_idx
    ON candle(market, timeframe, candle_start);
CREATE INDEX IF NOT EXISTS candle_source_idx
    ON candle(source_id, instrument_id);

CREATE TABLE IF NOT EXISTS sync_state (
    source_id      TEXT NOT NULL,
    instrument_id  TEXT NOT NULL,
    timeframe      TEXT NOT NULL,
    cursor         TEXT,             -- last synced candle_start (UTC ISO)
    last_sync_at   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ok',
    error_code     TEXT NOT NULL DEFAULT '',
    rows_written   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, instrument_id, timeframe)
);

CREATE TABLE IF NOT EXISTS revision (
    revision_id    TEXT PRIMARY KEY,
    instrument_id  TEXT NOT NULL,
    timeframe      TEXT NOT NULL,
    candle_start   TEXT NOT NULL,
    old_revision   INTEGER NOT NULL,
    new_revision   INTEGER NOT NULL,
    source_id      TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    at             TEXT NOT NULL
);
"""


class CandleStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
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
    def upsert_candles(self, candles: list[MarketCandle]) -> dict[str, int]:
        """Batch upsert — same key + higher revision supersedes."""
        inserted = superseded = rejected = 0
        db = self._db()
        with db:  # single transaction — batch write, not per-row commits
            for candle in candles:
                if candle.validate():
                    rejected += 1
                    continue
                existing = db.execute(  # sql-ok: per-row revision check in conditional upsert
                    "SELECT data_revision FROM candle WHERE instrument_id=?"
                    " AND timeframe=? AND candle_start=? AND adjustment_type=?",
                    (
                        candle.instrument_id, candle.timeframe,
                        candle.candle_start.isoformat(), candle.adjustment_type,
                    ),
                ).fetchone()
                if existing is not None:
                    if candle.data_revision <= int(existing["data_revision"]):
                        continue  # duplicate or older correction — drop
                    db.execute(  # sql-ok: conditional upsert in one txn
                        "INSERT INTO revision(revision_id, instrument_id,"
                        " timeframe, candle_start, old_revision, new_revision,"
                        " source_id, reason, at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            f"rev-{candle.instrument_id}-{int(candle.candle_start.timestamp())}",
                            candle.instrument_id, candle.timeframe,
                            candle.candle_start.isoformat(),
                            int(existing["data_revision"]), candle.data_revision,
                            candle.source_id, "source-correction",
                            utcnow().isoformat(),
                        ),
                    )
                    superseded += 1
                else:
                    inserted += 1
                db.execute(  # sql-ok: conditional upsert in one txn
                    "INSERT OR REPLACE INTO candle(instrument_id, market,"
                    " timeframe, candle_start, candle_end, open, high, low,"
                    " close, volume, turnover, currency, source_id,"
                    " data_revision, adjustment_type, received_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candle.instrument_id, candle.market, candle.timeframe,
                        candle.candle_start.isoformat(),
                        candle.candle_end.isoformat(),
                        str(candle.open), str(candle.high), str(candle.low),
                        str(candle.close), str(candle.volume),
                        str(candle.turnover), candle.currency,
                        candle.source_id, candle.data_revision,
                        candle.adjustment_type, utcnow().isoformat(),
                    ),
                )
        return {"inserted": inserted, "superseded": superseded, "rejected": rejected}

    # ------------------------------------------------------------------
    def candles(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        adjustment_type: str = "raw",
    ) -> list[MarketCandle]:
        sql = (
            "SELECT * FROM candle WHERE instrument_id=? AND timeframe=?"
            " AND adjustment_type=?"
        )
        params: list[Any] = [instrument_id, timeframe, adjustment_type]
        if start is not None:
            sql += " AND candle_start >= ?"
            params.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            sql += " AND candle_start <= ?"
            params.append(end.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY candle_start"
        out: list[MarketCandle] = []
        for row in self._db().execute(sql, params).fetchall():
            out.append(MarketCandle(
                instrument_id=row["instrument_id"], market=row["market"],
                timeframe=row["timeframe"],
                open=Decimal(row["open"]), high=Decimal(row["high"]),
                low=Decimal(row["low"]), close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                turnover=Decimal(row["turnover"]),
                currency=row["currency"],
                candle_start=datetime.fromisoformat(row["candle_start"]),
                candle_end=datetime.fromisoformat(row["candle_end"]),
                source_id=row["source_id"],
                data_revision=int(row["data_revision"]),
                adjustment_type=row["adjustment_type"],
            ))
        return out

    def cursor(self, source_id: str, instrument_id: str, timeframe: str) -> datetime | None:
        row = self._db().execute(
            "SELECT cursor FROM sync_state WHERE source_id=? AND"
            " instrument_id=? AND timeframe=?",
            (source_id, instrument_id, timeframe),
        ).fetchone()
        if row is None or not row["cursor"]:
            return None
        return datetime.fromisoformat(row["cursor"])

    def update_sync_state(
        self,
        source_id: str,
        instrument_id: str,
        timeframe: str,
        cursor: datetime | None,
        status: str,
        rows_written: int,
        error_code: str = "",
    ) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO sync_state(source_id, instrument_id,"
            " timeframe, cursor, last_sync_at, status, error_code, rows_written)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id, instrument_id, timeframe,
                cursor.isoformat() if cursor else None,
                utcnow().isoformat(), status, error_code, rows_written,
            ),
        )
        self._db().commit()

    def sync_states(self) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self._db().execute(
                "SELECT * FROM sync_state ORDER BY instrument_id"
            ).fetchall()
        ]

    def revisions(self, instrument_id: str | None = None) -> list[dict[str, Any]]:
        if instrument_id:
            rows = self._db().execute(
                "SELECT * FROM revision WHERE instrument_id=? ORDER BY at",
                (instrument_id,),
            )
        else:
            rows = self._db().execute("SELECT * FROM revision ORDER BY at")
        return [dict(r) for r in rows.fetchall()]

    def stats(self) -> dict[str, Any]:
        db = self._db()
        count = db.execute("SELECT COUNT(*) c FROM candle").fetchone()["c"]
        return {
            "candles": count,
            "db_path": str(self._path),
            "db_size_bytes": self._path.stat().st_size if self._path.exists() else 0,
        }


class HistoricalMarketDataService:
    """Incremental sync + integrity for historical candles."""

    def __init__(self, store: CandleStore, calendar: TradingCalendar) -> None:
        self._store = store
        self._calendar = calendar

    # ------------------------------------------------------------------
    async def sync(
        self,
        source: MarketDataSource,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Incremental sync — resumes from the stored cursor."""
        market = instrument_id.split(":", 1)[0] if ":" in instrument_id else "tw"
        cursor = self._store.cursor(source.capability.source_id, instrument_id, timeframe)
        try:
            candles = await source.fetch_history(
                instrument_id, timeframe, start, end, cursor=cursor
            )
        except Exception as exc:
            self._store.update_sync_state(
                source.capability.source_id, instrument_id, timeframe,
                cursor, "failed", 0, error_code=type(exc).__name__,
            )
            return {"ok": False, "error_code": "SOURCE_FETCH_FAILED"}

        result = self._store.upsert_candles(candles)
        new_cursor = max(
            (c.candle_start for c in candles), default=cursor
        )
        self._store.update_sync_state(
            source.capability.source_id, instrument_id, timeframe,
            new_cursor, "ok", result["inserted"] + result["superseded"],
        )
        gaps = self.gap_check(instrument_id, market, timeframe, start, end)
        return {
            "ok": True,
            **result,
            "cursor": new_cursor.isoformat() if new_cursor else None,
            "gaps": len(gaps),
        }

    # ------------------------------------------------------------------
    def gap_check(
        self,
        instrument_id: str,
        market: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjustment_type: str = "raw",
    ) -> list[str]:
        """Missing trading-day candles — never silently filled."""
        if timeframe != "1d":
            return []  # intraday gap detection needs session windows
        have = {
            c.candle_start.date()
            for c in self._store.candles(
                instrument_id, timeframe, start, end, adjustment_type
            )
        }
        expected = self._calendar.trading_days(
            market, start.date(), end.date()
        )
        return [d.isoformat() for d in expected if d not in have]

    def replay(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjustment_type: str = "raw",
    ) -> list[dict[str, Any]]:
        """Deterministic replay for backtests — explicit price type."""
        return [
            c.to_dict()
            for c in self._store.candles(
                instrument_id, timeframe, start, end, adjustment_type
            )
        ]
