"""MarketDataQuery — 星澄 read-only market data surface.

Exposed through the governed channel only. Every result carries data
timestamps and source status so the model can judge freshness itself.
This class has NO write methods — the query object structurally cannot
mutate the authoritative cache, swap sources, or fabricate prices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import AdjustmentType
from .engine import MarketDataEngine
from .history import CandleStore
from .corporate import CorporateActionService
from .calendar import TradingCalendar


class MarketDataQuery:
    """Read-only view for 星澄 — all results include provenance."""

    def __init__(
        self,
        engine: MarketDataEngine,
        store: CandleStore,
        calendar: TradingCalendar,
        corporate: CorporateActionService,
    ) -> None:
        self._engine = engine
        self._store = store
        self._calendar = calendar
        self._corporate = corporate

    # ------------------------------------------------------------------
    def quote(self, instrument_id: str) -> dict[str, Any]:
        data = self._engine.latest_quote(instrument_id)
        if data is None:
            return {"ok": False, "error_code": "NO_QUOTE"}
        data["source_status"] = self._engine.source_status(data["source_id"])
        return {"ok": True, "quote": data}

    def quotes(self, instrument_ids: list[str]) -> dict[str, Any]:
        return {
            "ok": True,
            "quotes": {
                iid: self._engine.latest_quote(iid) for iid in instrument_ids
            },
            "queried_at": datetime.now().astimezone().isoformat(),
        }

    def history(
        self,
        instrument_id: str,
        timeframe: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        adjustment_type: str = "raw",
        as_of: datetime | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        candles = self._store.candles(
            instrument_id, timeframe, start, end,
            adjustment_type=AdjustmentType.RAW.value,
        )
        if adjustment_type != AdjustmentType.RAW.value:
            candles = self._corporate.adjust(
                candles, adjustment_type,
                as_of=as_of.date() if as_of else None,
            )
        return {
            "ok": True,
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "adjustment_type": adjustment_type,
            "candles": [c.to_dict() for c in candles[-limit:]],
            "count": min(len(candles), limit),
            "sources": {c.source_id for c in candles} and sorted(
                {c.source_id for c in candles}
            ),
        }

    def trend(self, instrument_id: str, days: int = 20) -> dict[str, Any]:
        """Simple trend evidence for the model — read-only derivation."""
        candles = self._store.candles(instrument_id, "1d")
        if not candles:
            return {"ok": False, "error_code": "NO_HISTORY"}
        tail = candles[-max(1, int(days)):]
        first, last = tail[0].close, tail[-1].close
        change_pct = (
            (last - first) / first * 100 if first else None
        )
        volumes = [c.volume for c in tail]
        avg_volume = sum(volumes) / len(volumes) if volumes else None
        return {
            "ok": True,
            "instrument_id": instrument_id,
            "days": len(tail),
            "first_close": str(first),
            "last_close": str(last),
            "change_pct": str(change_pct) if change_pct is not None else None,
            "avg_volume": str(avg_volume) if avg_volume is not None else None,
            "latest_source": tail[-1].source_id,
            "latest_candle": tail[-1].candle_start.isoformat(),
        }

    def market_session(self, market: str, at: datetime | None = None) -> dict[str, Any]:
        at = at or datetime.now().astimezone()
        return {
            "ok": True,
            "market": market,
            "session": self._calendar.session_for(market, at),
            "open": self._calendar.is_open(market, at),
            "at_utc": at.astimezone().isoformat(),
        }

    def source_health(self) -> dict[str, Any]:
        return {"ok": True, "sources": self._engine.source_status()}
