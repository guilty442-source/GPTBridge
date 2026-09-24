"""PortfolioPerformanceEngine — PnL, TWR, MWR.

- Period PnL from valuation deltas minus external cash flows (a deposit
  is never counted as profit).
- TWR: chain-linked sub-period returns between external flows —
  compares portfolio performance independent of contribution timing.
- MWR: IRR over dated cash flows — the owner's actual return given
  when money went in/out.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

DAY = 86400


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioPerformanceEngine:
    def __init__(self, ledger: Any, income: Any) -> None:
        self._ledger = ledger
        self._income = income

    # ------------------------------------------------------------------
    def _flows(self, since: float = 0.0) -> list[dict[str, Any]]:
        """External cash flows (deposits/withdrawals/remittance/fx)."""
        flows = []
        for t in self._ledger.list():
            tt = t.get("transaction_type")
            if tt in ("remittance", "adjustment", "fx_exchange"):
                amt = _d(t.get("net_amount") or t.get("amount")
                         or t.get("gross_amount"))
                if tt == "adjustment" and str(
                        t.get("adjustment_kind")) != "external_flow":
                    continue
                ts = float(t.get("traded_at") or
                           t.get("transaction_date") or 0)
                if ts >= since:
                    flows.append({"at": ts, "amount": amt,
                                  "currency": t.get("currency", "")})
        return sorted(flows, key=lambda f: f["at"])

    # ------------------------------------------------------------------
    def pnl_summary(self, *, current_value,
                    invested_capital, realized_pnl,
                    unrealized_pnl, income_total,
                    fees_total, fx_cost_total="0") -> dict[str, Any]:
        cv, ic = _d(current_value), _d(invested_capital)
        rp, up = _d(realized_pnl), _d(unrealized_pnl)
        inc, fees, fxc = _d(income_total), _d(fees_total), _d(fx_cost_total)
        total_change = cv - ic
        return {
            "ok": True,
            "total_assets": str(cv),
            "invested_capital": str(ic),
            "asset_change": str(total_change),
            "realized_pnl": str(rp),
            "unrealized_pnl": str(up),
            "income_total": str(inc),
            "fees_total": str(fees),
            "fx_cost_total": str(fxc),
            "total_return": str(rp + up + inc - fxc),
            "note": "asset_change includes external flows; "
                    "total_return excludes them",
        }

    # ------------------------------------------------------------------
    def twr(self, valuations: list[dict[str, Any]],
            flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """valuations: [{at, value}] ascending; flows: [{at, amount}]."""
        if len(valuations) < 2:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        flows = sorted(flows or self._flows(), key=lambda f: f["at"])
        linked = Decimal(1)
        prev_at, prev_v = float(valuations[0]["at"]), _d(
            valuations[0]["value"])
        for v in valuations[1:]:
            at, val = float(v["at"]), _d(v["value"])
            # flows inside (prev_at, at] adjust the base
            flow = sum(f["amount"] for f in flows
                       if prev_at < f["at"] <= at)
            denom = prev_v + flow
            if denom > 0:
                linked *= (val / denom)
            prev_at, prev_v = at, val
        return {"ok": True, "twr": str(linked - 1),
                "periods": len(valuations) - 1}

    def mwr(self, valuations: list[dict[str, Any]],
            flows: list[dict[str, Any]] | None = None,
            *, years: float | None = None) -> dict[str, Any]:
        """IRR over dated flows; bisection on NPV."""
        if len(valuations) < 1:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        flows = sorted(flows or self._flows(), key=lambda f: f["at"])
        t0 = flows[0]["at"] if flows else float(valuations[0]["at"])
        t_end = float(valuations[-1]["at"])
        end_v = _d(valuations[-1]["value"])

        def npv(rate: float) -> float:
            # PV at t0: end value is an inflow to the investor, dated
            # contributions are outflows — solve for the rate where the
            # two present values are equal.
            total = float(end_v) / (1 + rate) ** (
                max((t_end - t0) / (365 * DAY), 1e-9))
            for f in flows:
                total -= float(f["amount"]) / (1 + rate) ** (
                    max((f["at"] - t0) / (365 * DAY), 0))
            return total

        lo, hi = -0.9999, 10.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if npv(mid) > 0:
                lo = mid
            else:
                hi = mid
        r = (lo + hi) / 2
        return {"ok": True, "mwr": str(Decimal(str(round(r, 8)))),
                "flows": len(flows)}

    # ------------------------------------------------------------------
    def period_pnl(self, valuations: list[dict[str, Any]],
                   window: str) -> dict[str, Any]:
        """today|week|month|year|all — value change minus net flows."""
        days = {"today": 1, "week": 7, "month": 30,
                "year": 365, "all": 10 ** 9}.get(window)
        if days is None or not valuations:
            return {"ok": False, "error_code": "WINDOW_UNKNOWN"}
        cutoff = time.time() - days * DAY
        inside = [v for v in valuations if float(v["at"]) >= cutoff]
        if not inside:
            return {"ok": False, "error_code": "NO_VALUATION_IN_WINDOW"}
        start_v = _d(inside[0]["value"])
        end_v = _d(inside[-1]["value"])
        net_flow = sum(f["amount"] for f in self._flows()
                       if inside[0]["at"] < f["at"] <= inside[-1]["at"])
        return {"ok": True, "window": window,
                "start_value": str(start_v), "end_value": str(end_v),
                "external_flows": str(net_flow),
                "pnl": str(end_v - start_v - net_flow)}
