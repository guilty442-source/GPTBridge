from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from .analytics_common import (
    _decoded_json,
    _json,
    number,
    parse_datetime,
    protect_text,
    rounded,
    unprotect_text,
    utc_now,
    utc_text,
)


class AnalyticsStoreMarketDataMixin:
    """Price bars and market event persistence methods."""

    def add_price_bars(self, bars: Iterable[dict[str, Any]]) -> int:
        prepared: list[tuple[Any, ...]] = []
        for bar in bars:
            symbol = str(bar.get("symbol") or "").strip().upper()
            observed = parse_datetime(bar.get("observed_at") or bar.get("timestamp"))
            close = number(bar.get("close"), -1)
            if not symbol or observed is None or close <= 0:
                continue
            prepared.append(
                (
                    symbol,
                    utc_text(observed),
                    number(bar.get("open"), close),
                    number(bar.get("high"), close),
                    number(bar.get("low"), close),
                    close,
                    number(bar.get("volume")),
                    str(bar.get("currency") or "").upper(),
                    str(bar.get("provider") or "manual"),
                    int(bool(bar.get("verified", True))),
                )
            )
        if not prepared:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO prices(symbol, observed_at, open, high, low, close, volume, currency, provider, verified)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, observed_at, provider) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, currency=excluded.currency,
                    verified=excluded.verified
                """,
                prepared,
            )
        return len(prepared)

    def price_series(self, symbol: str, limit: int = 800) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, open, high, low, close, volume, currency, provider, verified
                FROM prices WHERE symbol = ? ORDER BY observed_at DESC LIMIT ?
                """,
                (symbol.strip().upper(), max(2, min(5000, limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_prices(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.* FROM prices p
                INNER JOIN (
                    SELECT symbol, MAX(observed_at) AS observed_at FROM prices GROUP BY symbol
                ) latest ON latest.symbol=p.symbol AND latest.observed_at=p.observed_at
                ORDER BY p.verified DESC, p.provider
                """
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output.setdefault(str(row["symbol"]), dict(row))
        return output

    def add_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._normalized_event(payload)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT event_id, dedupe_key, event_type, symbol, title, scheduled_at, source, source_url_encrypted, sentiment, confidence, status, details_encrypted, created_at FROM market_events WHERE dedupe_key = ?",
                (row["dedupe_key"],),
            ).fetchone()
            if existing is not None:
                existing_details = _decoded_json(
                    unprotect_text(str(existing["details_encrypted"] or "")),
                    {},
                )
                if (
                    existing["sentiment"] == row["sentiment"]
                    and existing["confidence"] == row["confidence"]
                    and existing["status"] == row["status"]
                    and existing_details == (payload.get("details") or {})
                ):
                    return self._public_event(existing)
            connection.execute(
                """
                INSERT INTO market_events VALUES(
                    :event_id, :dedupe_key, :event_type, :symbol, :title,
                    :scheduled_at, :source, :source_url_encrypted, :sentiment,
                    :confidence, :status, :details_encrypted, :created_at
                ) ON CONFLICT(dedupe_key) DO UPDATE SET
                    sentiment=excluded.sentiment, confidence=excluded.confidence,
                    status=excluded.status, details_encrypted=excluded.details_encrypted
                """,
                row,
            )
        return self._public_event(row)

    def _normalized_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        scheduled = parse_datetime(payload.get("scheduled_at") or payload.get("published_at")) or utc_now()
        symbol = str(payload.get("symbol") or "").strip().upper()
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("event title is required")
        event_type = str(payload.get("event_type") or "news").strip().lower()
        source = str(payload.get("source") or "manual").strip()
        dedupe_source = str(payload.get("dedupe_key") or f"{event_type}|{symbol}|{title}|{utc_text(scheduled)[:16]}")
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        return {
            "event_id": event_id,
            "dedupe_key": dedupe_key,
            "event_type": event_type,
            "symbol": symbol,
            "title": title,
            "scheduled_at": utc_text(scheduled),
            "source": source,
            "source_url_encrypted": protect_text(str(payload.get("source_url") or payload.get("url") or "")),
            "sentiment": rounded(number(payload.get("sentiment")), 4) if payload.get("sentiment") is not None else None,
            "confidence": max(0.0, min(1.0, number(payload.get("confidence"), 0.5))),
            "status": str(payload.get("status") or "scheduled"),
            "details_encrypted": protect_text(_json(payload.get("details") or {})),
            "created_at": utc_text(),
        }

    def add_events(
        self,
        payloads: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist a group of events with one encrypted database write."""

        normalized = [dict(payload) for payload in payloads]
        if not normalized:
            return []
        with self.batch_updates():
            return [self.add_event(payload) for payload in normalized]

    @staticmethod
    def _public_event(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_url"] = unprotect_text(str(item.pop("source_url_encrypted", "") or ""))
        details = unprotect_text(str(item.pop("details_encrypted", "") or ""))
        item["details"] = _decoded_json(details, {})
        return item

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, dedupe_key, event_type, symbol, title, scheduled_at, source, source_url_encrypted, sentiment, confidence, status, details_encrypted, created_at FROM market_events ORDER BY scheduled_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [self._public_event(row) for row in rows]


__all__ = ['AnalyticsStoreMarketDataMixin']
