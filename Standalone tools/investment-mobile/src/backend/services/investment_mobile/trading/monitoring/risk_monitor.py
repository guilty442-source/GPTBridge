"""PortfolioRiskMonitor + CrossMarketExposureMonitor — measurement and
warning only. These wrap the asset-layer analytics; the formal trading
RiskEngine stays the sole trade-decision authority.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioRiskMonitor:
    """Advisory risk alarms — emits events, decides nothing."""

    def __init__(self, risk_analytics: Any, events: Any,
                 events_limits: dict[str, float] | None = None) -> None:
        self._ra = risk_analytics
        self._events = events
        self._limits = {                     # user-adjustable warnings
            "top_position": 0.25, "hhi": 0.20,
            "max_drawdown": -0.15,
            **(events_limits or {})}

    def check(self, valuation: dict[str, Any],
              curve: list[dict[str, Any]]) -> dict[str, Any]:
        fired = []
        conc = self._ra.concentration(valuation)
        if conc.get("ok"):
            top = _d(conc["top_position_weight"])
            if top > _d(self._limits["top_position"]):
                fired.append(self._events.emit(
                    "concentration",
                    instrument_id=conc["top_position"],
                    severity="WARNING",
                    detail={"top_weight": str(top),
                            "limit": str(self._limits["top_position"])}))
            if _d(conc["hhi"]) > _d(self._limits["hhi"]):
                fired.append(self._events.emit(
                    "concentration", severity="WARNING",
                    detail={"hhi": conc["hhi"]}))
        if len(curve) >= 2:
            dd = self._ra.max_drawdown(curve)
            if _d(dd.get("max_drawdown", "0")) <= _d(
                    self._limits["max_drawdown"]):
                fired.append(self._events.emit(
                    "drawdown", severity="CRITICAL",
                    detail={"max_drawdown": dd["max_drawdown"]}))
        return {"ok": True, "alerts": [f for f in fired if f.get("ok")],
                "authority": "warning only — formal RiskEngine decides"}


class CrossMarketExposureMonitor:
    """Look-through across TW/US/ETF/fund — incomplete data flagged,
    unknown exposure is never zero."""

    def __init__(self, exposure: Any, events: Any) -> None:
        self._exposure = exposure
        self._events = events

    def check(self, valuation: dict[str, Any]) -> dict[str, Any]:
        r = self._exposure.analyze(valuation)
        if not r.get("ok"):
            return r
        baskets = set(r.get("baskets", {}))
        stale = [k for k, v in r.get("baskets", {}).items()
                 if v.get("stale")]
        # fund/ETF-like positions with no registered basket → unknown
        # composition → PARTIAL_COVERAGE, never silently zero
        uncovered = []
        for p in valuation.get("positions", []):
            iid = p["instrument_id"]
            composite = (iid.startswith("fund:")
                         or p.get("market") == "fund")
            if composite and iid not in baskets:
                uncovered.append(iid)
        coverage = "FULL"
        if uncovered:
            coverage = "PARTIAL_COVERAGE"
        if stale:
            coverage = "STALE" if coverage == "FULL" else coverage
        if uncovered or stale:
            self._events.emit(
                "exposure_overlap", severity="NOTICE",
                detail={"coverage": coverage,
                        "uncovered": uncovered, "stale": stale})
        return {"ok": True, **r, "coverage": coverage,
                "uncovered_baskets": uncovered,
                "note": "unknown overlap is never treated as zero"}
