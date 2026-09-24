"""MonitoringDataGate — data validity before any analysis runs.

Statuses: VALID | STALE | INCOMPLETE | UNAVAILABLE.

Insufficient data still allows historical research — but the gate marks
``executable=False`` so no tradeable proposal can be claimed from it.
"""

from __future__ import annotations

import time
from typing import Any

STALE_QUOTE_S = 3 * 86400      # daily bars: >3 days old = stale
STALE_NAV_S = 7 * 86400        # fund NAVs publish less frequently
STALE_FX_S = 7 * 86400
STALE_POSITION_S = 30 * 86400  # manual holdings


class MonitoringDataGate:
    def __init__(self, market_engine: Any, candle_store: Any,
                 fx: Any, fund_engine: Any | None = None) -> None:
        self._market = market_engine
        self._candles = candle_store
        self._fx = fx
        self._fund = fund_engine

    # ------------------------------------------------------------------
    def _candle_age(self, instrument_id: str) -> tuple[str, float | None]:
        try:
            row = self._candles.latest(instrument_id)
        except Exception:
            row = None
        if row is None:
            return "UNAVAILABLE", None
        ts = float(row.candle_end.timestamp())
        age = time.time() - ts
        return ("STALE" if age > STALE_QUOTE_S else "VALID"), ts

    def check_instrument(self, instrument_id: str,
                         market: str = "") -> dict[str, Any]:
        status, ts = self._candle_age(instrument_id)
        return {"instrument_id": instrument_id, "market": market,
                "status": status, "data_timestamp": ts,
                "executable": status == "VALID"}

    def check_fund(self, fund_id: str,
                   share_class_id: str = "A") -> dict[str, Any]:
        nav_date = None
        if self._fund is not None:
            try:
                nav = self._fund.nav.latest_published(
                    fund_id, share_class_id)
                if nav.get("ok"):
                    import datetime as _dt
                    nav_date = _dt.date.fromisoformat(
                        nav["nav"]["nav_date"]).toordinal()
            except Exception:
                nav_date = None
        if not nav_date:
            return {"fund_id": fund_id, "status": "UNAVAILABLE",
                    "executable": False}
        import datetime as _dt
        age_s = (time.time() -
                 _dt.datetime.fromordinal(nav_date).replace(
                     tzinfo=_dt.timezone.utc).timestamp())
        st = "STALE" if age_s > STALE_NAV_S else "VALID"
        return {"fund_id": fund_id, "status": st,
                "nav_date": nav_date, "executable": st == "VALID",
                "note": "NAV 是已公告淨值——非即時成交價"}

    def check_fx(self, base: str = "USD", quote: str = "TWD"
                 ) -> dict[str, Any]:
        r = self._fx.latest(base, quote) or self._fx.latest(quote, base)
        if r is None:
            return {"pair": f"{base}->{quote}", "status": "UNAVAILABLE",
                    "executable": False}
        age = (time.time() - r.observed_at.timestamp())
        st = "STALE" if age > STALE_FX_S else "VALID"
        return {"pair": f"{base}->{quote}", "status": st,
                "data_timestamp": r.observed_at.timestamp(),
                "executable": st == "VALID"}

    def check_positions(self, positions: list[dict[str, Any]]
                        ) -> dict[str, Any]:
        if not positions:
            return {"status": "UNAVAILABLE", "executable": False,
                    "positions": 0}
        oldest = min(float(p.get("valuation_timestamp") or 0)
                     for p in positions)
        age = time.time() - oldest
        st = "STALE" if age > STALE_POSITION_S else "VALID"
        return {"status": st, "positions": len(positions),
                "oldest_update": oldest, "executable": st == "VALID"}

    def evaluate(self, instrument_ids: list[str],
                 *, market: str = "") -> dict[str, Any]:
        checks = [self.check_instrument(i, market)
                  for i in instrument_ids]
        worst = "VALID"
        order = {"VALID": 0, "STALE": 1, "INCOMPLETE": 2,
                 "UNAVAILABLE": 3}
        for c in checks:
            if order[c["status"]] > order[worst]:
                worst = c["status"]
        return {"ok": True, "overall": worst,
                "checks": checks,
                "executable": worst == "VALID",
                "note": ("insufficient data → historical research only; "
                         "no executable proposal may be claimed"
                         if worst != "VALID" else "")}
