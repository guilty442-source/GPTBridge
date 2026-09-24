"""PortfolioRiskAnalytics — measurement only, no trading authority.

Concentration, volatility, max drawdown, pairwise correlation over the
snapshot series, and risk-budget usage. This engine REPORTS — the
decisioning risk engines (``risk_engine`` for orders, ``live.risk`` for
LIVE) are untouched; nothing here can veto or approve a trade.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioRiskAnalytics:
    def __init__(self, snapshots: Any | None = None) -> None:
        self._snapshots = snapshots

    # ------------------------------------------------------------------
    def concentration(self, valuation: dict[str, Any]) -> dict[str, Any]:
        total = _d(valuation.get("total_assets") or "0")
        if total <= 0:
            return {"ok": False, "error_code": "EMPTY_PORTFOLIO"}
        items = sorted(
            ((p["instrument_id"], _d(p.get("market_value_display")
                                     or p.get("market_value")))
             for p in valuation.get("positions", [])),
            key=lambda kv: kv[1], reverse=True)
        top = items[0][1] / total if items else Decimal(0)
        hhi = sum((v / total) ** 2 for _, v in items)
        return {"ok": True,
                "top_position_weight": str(top),
                "top_position": items[0][0] if items else "",
                "hhi": str(hhi),
                "positions": len(items),
                "weights": {k: str(v / total) for k, v in items}}

    # ------------------------------------------------------------------
    def _returns(self, values: list[Decimal]) -> list[float]:
        return [float(values[i] / values[i - 1] - 1)
                for i in range(1, len(values)) if values[i - 1] > 0]

    def volatility(self, valuations: list[dict[str, Any]],
                   *, annualize: bool = True) -> dict[str, Any]:
        vals = [_d(v["value"]) for v in valuations]
        rets = self._returns(vals)
        if len(rets) < 2:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if annualize:
            sd *= math.sqrt(252)
        return {"ok": True, "volatility": str(round(sd, 8)),
                "observations": len(rets)}

    def max_drawdown(self,
                     valuations: list[dict[str, Any]]) -> dict[str, Any]:
        vals = [_d(v["value"]) for v in valuations]
        peak, mdd = Decimal(0), Decimal(0)
        for v in vals:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1)
        return {"ok": True, "max_drawdown": str(mdd)}

    def correlation(self, series_a: list[dict[str, Any]],
                    series_b: list[dict[str, Any]]) -> dict[str, Any]:
        ra = self._returns([_d(v["value"]) for v in series_a])
        rb = self._returns([_d(v["value"]) for v in series_b])
        n = min(len(ra), len(rb))
        if n < 2:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        ra, rb = ra[-n:], rb[-n:]
        ma, mb = sum(ra) / n, sum(rb) / n
        cov = sum((a - ma) * (b - mb) for a, b in zip(ra, rb))
        va = math.sqrt(sum((a - ma) ** 2 for a in ra))
        vb = math.sqrt(sum((b - mb) ** 2 for b in rb))
        corr = cov / (va * vb) if va * vb > 0 else 0.0
        return {"ok": True, "correlation": str(round(corr, 6)),
                "observations": n}

    def report(self, valuation: dict[str, Any],
               valuations: list[dict[str, Any]]) -> dict[str, Any]:
        out = {"ok": True}
        out["concentration"] = self.concentration(valuation)
        if len(valuations) >= 2:
            out["volatility"] = self.volatility(valuations)
            out["max_drawdown"] = self.max_drawdown(valuations)
        out["authority"] = "measurement-only — no trade decisions"
        return out
