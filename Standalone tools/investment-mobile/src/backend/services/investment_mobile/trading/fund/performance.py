"""FundPerformanceEngine — period returns, risk metrics.

Computed from published NAV history (+ distributions for total return).
Insufficient data reports INSUFFICIENT_DATA — periods are never
fabricated. Comparisons must use the same period, currency and return
basis; mismatched inputs are rejected, not silently mixed.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .contracts import FundNAV, NavType
from .distribution import FundDistributionService
from .nav import FundNAVService

_PERIODS = {
    "1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1095, "5y": 1826,
}
_TRADING_DAYS_PER_YEAR = 252


class FundPerformanceEngine:
    def __init__(
        self, nav: FundNAVService, distributions: FundDistributionService,
    ) -> None:
        self._nav = nav
        self._dist = distributions

    # ------------------------------------------------------------------
    def _series(
        self, fund_id: str, share_class_id: str,
        include_distributions: bool = False,
    ) -> list[tuple[date, Decimal]]:
        navs = self._nav.history(fund_id, share_class_id,
                                 nav_type=NavType.PUBLISHED.value)
        if not include_distributions:
            return [(n.nav_date, n.nav) for n in navs]
        adjusted = self._dist.total_return_navs(navs, share_class_id)
        return [
            (date.fromisoformat(r["nav_date"]),
             Decimal(r["total_return_value"]))
            for r in adjusted
        ]

    def period_return(
        self, fund_id: str, share_class_id: str, period: str,
        include_distributions: bool = False,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Cumulative return over a named period (or 'inception')."""
        as_of = as_of or date.today()
        series = [
            (d, v) for d, v in
            self._series(fund_id, share_class_id, include_distributions)
            if d <= as_of
        ]
        if not series:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        end_d, end_v = series[-1]
        if period == "inception":
            start_d, start_v = series[0]
        else:
            days = _PERIODS.get(period)
            if days is None:
                return {"ok": False, "error_code": "PERIOD_UNKNOWN"}
            cutoff = as_of - timedelta(days=days)
            earlier = [(d, v) for d, v in series if d <= cutoff]
            if not earlier:
                return {"ok": False, "error_code": "INSUFFICIENT_DATA",
                        "earliest": series[0][0].isoformat()}
            start_d, start_v = earlier[-1]
        if start_v <= 0:
            return {"ok": False, "error_code": "BAD_NAV"}
        ret = (end_v - start_v) / start_v
        actual_days = (end_d - start_d).days
        return {
            "ok": True, "period": period,
            "start": start_d.isoformat(), "end": end_d.isoformat(),
            "actual_days": actual_days,
            "return": str(ret),
            "return_pct": str(ret * 100),
            "annualized": str(
                (Decimal(1) + ret) ** (Decimal(365) / actual_days) - 1
            ) if actual_days > 0 else "0",
            "include_distributions": include_distributions,
        }

    def all_periods(
        self, fund_id: str, share_class_id: str,
        include_distributions: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "periods": {
                p: self.period_return(fund_id, share_class_id, p,
                                      include_distributions)
                for p in (*_PERIODS, "inception")
            },
            "include_distributions": include_distributions,
        }

    # ------------------------------------------------------------------
    def risk_metrics(
        self, fund_id: str, share_class_id: str,
        risk_free_annual: Decimal | str | float = Decimal("0.01"),
        include_distributions: bool = False,
    ) -> dict[str, Any]:
        """Volatility / max drawdown / Sharpe / Sortino on daily returns."""
        series = self._series(fund_id, share_class_id, include_distributions)
        if len(series) < 10:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA",
                    "points": len(series)}
        rets = [
            float(b / a - 1) for (_, a), (_, b)
            in zip(series, series[1:]) if a > 0
        ]
        if not rets:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        vol_daily = math.sqrt(var)
        vol_annual = vol_daily * math.sqrt(_TRADING_DAYS_PER_YEAR)
        peak = series[0][1]
        max_dd = Decimal("0")
        for _, v in series:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak else Decimal("0")
            max_dd = max(max_dd, dd)
        rf = float(risk_free_annual)
        rf_daily = rf / _TRADING_DAYS_PER_YEAR
        excess = [r - rf_daily for r in rets]
        downside = [min(r - rf_daily, 0.0) for r in rets]
        downside_dev = math.sqrt(
            sum(d * d for d in downside) / len(downside))
        mean_ex = sum(excess) / len(excess)
        return {
            "ok": True, "points": len(series),
            "volatility_annual": str(vol_annual),
            "max_drawdown": str(max_dd),
            "sharpe": str(
                mean_ex / vol_daily * math.sqrt(_TRADING_DAYS_PER_YEAR)
                if vol_daily else 0),
            "sortino": str(
                mean_ex / downside_dev * math.sqrt(_TRADING_DAYS_PER_YEAR)
                if downside_dev else 0),
            "include_distributions": include_distributions,
        }
