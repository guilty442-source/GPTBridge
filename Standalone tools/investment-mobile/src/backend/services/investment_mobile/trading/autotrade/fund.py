"""FundStrategyCoordinator — fund research on the NAV cycle.

Funds don't use the equity intraday pipeline. Analysis runs on
published-NAV updates: performance, risk, recurring-investment research,
rebalancing research and switch research. 星澄 produces
subscribe/add/hold/reduce/redeem/switch *advice* — this phase executes
no real fund trades. Fund PAPER simulations must use the fund pricing /
settlement model (published NAV basis), never equity market fills.
"""

from __future__ import annotations

import time
from typing import Any


class FundStrategyCoordinator:
    def __init__(self, fund_engine: Any, recommend: Any,
                 events: Any) -> None:
        self._fund = fund_engine
        self._recommend = recommend          # monitoring rec engine
        self._events = events

    # ------------------------------------------------------------------
    def analyze(self, fund_id: str, share_class_id: str = "A",
                *, position: dict[str, Any] | None = None
                ) -> dict[str, Any]:
        nav = self._fund.nav.latest_published(fund_id, share_class_id)
        if not nav.get("ok"):
            return {"ok": False, "error_code": "NO_NAV",
                    "note": "無已公告淨值——不得用估計值替代"}
        hist = self._fund.nav.history(fund_id, share_class_id)
        closes = [float(n.nav) for n in hist[-60:]] if hist else []
        indicators = {}
        if len(closes) >= 15:
            from ..intelligence.indicators import rsi
            indicators["rsi14"] = rsi(closes)
            indicators["last"] = closes[-1]
        rec = self._recommend.recommend(
            instrument_id=fund_id, kind="fund",
            indicators=indicators, position=position,
            nav=nav["nav"],
            data_timestamp=time.time()
            - nav.get("age_days", 0) * 86400)
        return {"ok": True, "fund_id": fund_id,
                "nav": nav["nav"], "nav_date": nav["nav"]["nav_date"],
                "nav_stale": nav.get("stale", False),
                "age_days": nav.get("age_days"),
                "recommendation": rec.get("recommendation"),
                "pricing_model": "published-nav (基金專屬計價)",
                "note": "基金不適用股票即時交易流程；本階段無真實基金交易"}

    def nav_update(self, fund_id: str,
                   share_class_id: str = "A") -> dict[str, Any]:
        """Event handler for FUND_NAV_UPDATED — emits a monitoring event
        then runs NAV-cycle analysis."""
        nav = self._fund.nav.latest_published(fund_id, share_class_id)
        if not nav.get("ok"):
            return {"ok": False, "error_code": "NO_NAV"}
        self._events.emit("nav_update", instrument_id=fund_id,
                          market="fund", severity="INFO",
                          detail={"nav": nav["nav"]["nav"],
                                  "nav_date": nav["nav"]["nav_date"]})
        return self.analyze(fund_id, share_class_id)
