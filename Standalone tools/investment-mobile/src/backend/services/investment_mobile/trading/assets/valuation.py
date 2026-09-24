"""PortfolioValuationEngine — honest multi-source valuation.

Each position is valued with ITS OWN price timestamp (TW close, US
close, fund NAV publish time can all differ). The aggregate reports the
per-source valuation times and a ``reference_only`` flag when data is
stale — different-time valuations are never disguised as a single
precise point-in-time asset state.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

STALE_SECONDS = 3 * 86400   # positions w/ older marks flagged


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioValuationEngine:
    def __init__(self, positions: Any, cash: Any, ccy: Any,
                 candle_store: Any | None = None) -> None:
        self._positions = positions
        self._cash = cash
        self._ccy = ccy
        self._candles = candle_store

    # ------------------------------------------------------------------
    def _price_for(self, instrument_id: str,
                   prices: dict[str, Any]) -> tuple[Decimal | None,
                                                  float | None]:
        """(price, observed_at) — manual quote first, then candle close."""
        if instrument_id in prices:
            p = prices[instrument_id]
            if isinstance(p, dict):
                return _d(p["price"]), float(p.get("at") or time.time())
            return _d(p), time.time()
        if self._candles is not None:
            try:
                row = self._candles.latest(instrument_id)
                if row is not None:
                    return _d(row.close), float(
                        row.candle_end.timestamp())
            except Exception:
                pass
        return None, None

    # ------------------------------------------------------------------
    def value(self, *, display_currency: str = "TWD",
              prices: dict[str, Any] | None = None,
              prices_at: dict[str, float] | None = None
              ) -> dict[str, Any]:
        display = display_currency.upper()
        prices = prices or {}
        positions = self._positions.list_positions()
        now = time.time()
        per_pos: list[dict[str, Any]] = []
        per_account: dict[str, Decimal] = {}
        per_market: dict[str, Decimal] = {}
        per_currency: dict[str, Decimal] = {}
        valuation_times: dict[str, float] = {}
        stale = False

        for p in positions:
            qty = _d(p["quantity"])
            price, observed = self._price_for(p["instrument_id"], prices)
            cur = p["cost_currency"] or p["market_rules"].get(
                "currency") or display
            if price is None:
                # no mark — fall back to cost, flagged estimated
                value = qty * _d(p["average_cost"])
                status, vtime = "estimated", float(
                    p.get("valuation_timestamp") or 0)
            else:
                value = qty * price
                status, vtime = "valued", float(observed or now)
            if now - vtime > STALE_SECONDS:
                status = "stale"
                stale = True
            conv = self._ccy.convert(value, cur, display)
            disp = _d(conv["amount"]) if conv.get("ok") else None
            basis = qty * _d(p["average_cost"])
            unreal = (value - basis) if price is not None else None
            per_pos.append({
                **p, "market_price": str(price) if price else "",
                "market_value": str(value),
                "market_value_display": str(disp) if disp is not None
                else "",
                "unrealized_pnl": str(unreal) if unreal is not None
                else "",
                "valuation_timestamp": vtime,
                "data_status": status,
            })
            if disp is not None:
                aid = p["account_id"]
                per_account[aid] = per_account.get(aid, Decimal(0)) + disp
                mkt = p.get("market") or "unknown"
                per_market[mkt] = per_market.get(mkt, Decimal(0)) + disp
                per_currency[cur] = per_currency.get(
                    cur, Decimal(0)) + value
                src = mkt
                valuation_times[src] = max(
                    valuation_times.get(src, 0), vtime)

        cash_total = Decimal(0)
        cash_detail: dict[str, dict[str, Any]] = {}
        for acct in self._positions._offline.list_accounts():
            aid = acct["account_id"]
            view = self._cash.balance_view(aid)
            cash_detail[aid] = view["balances"]
            for cur, b in view["balances"].items():
                conv = self._ccy.convert(_d(b["balance"]), cur, display)
                if conv.get("ok"):
                    amt = _d(conv["amount"])
                    cash_total += amt
                    per_account[aid] = per_account.get(
                        aid, Decimal(0)) + amt
                    per_currency[cur] = per_currency.get(
                        cur, Decimal(0)) + _d(b["balance"])

        return {
            "ok": True,
            "display_currency": display,
            "positions": per_pos,
            "per_account": {k: str(v) for k, v in per_account.items()},
            "per_market": {k: str(v) for k, v in per_market.items()},
            "per_currency": {k: str(v) for k, v in
                             per_currency.items()},
            "cash_detail": cash_detail,
            "total_invested_value": str(sum(per_market.values())),
            "total_market_value": str(sum(per_market.values())),
            "total_cash": str(cash_total),
            "total_assets": str(sum(per_account.values())),
            "valuation_times": {k: v for k, v in valuation_times.items()},
            "valued_at": now,
            "stale_data": stale,
            "reference_only": stale or not prices,
            "note": "each position carries its own valuation time — "
                    "aggregates mix source timestamps honestly",
        }
