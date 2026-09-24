"""UnifiedInvestmentPortfolio — cross-account offline asset center.

Aggregates offline accounts (國泰台股 / 富邦美股複委託 / 共同基金) and
cash into one read-only view:

- totals, per-market, per-account breakdown
- cost basis, realized PnL (from recorded transactions), unrealized PnL
  (requires current prices — manual quotes only this phase)
- cumulative dividends, allocation, currency exposure

Accounts stay independent — aggregation is a *view*, never a merge of
ledgers. Display currency conversion goes through CurrencyRateService
and always reports the FX rate's actual observed date; a missing rate
marks the figure ``fx_unavailable`` instead of inventing a rate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .accounts import OfflineAccountService


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class UnifiedInvestmentPortfolio:
    def __init__(self, accounts: OfflineAccountService,
                 fx: Any) -> None:
        self._accounts = accounts
        self._fx = fx

    # ------------------------------------------------------------------
    def _convert(self, amount: Decimal, from_c: str, to_c: str,
                 meta: dict[str, Any]) -> Decimal | None:
        if from_c == to_c:
            return amount
        r = self._fx.convert(amount, from_c, to_c)
        if not r.get("ok"):
            meta["fx_unavailable"] = True
            meta.setdefault("missing_pairs", set()).add(
                f"{from_c}->{to_c}")
            return None
        meta.setdefault("fx_rates", []).append({
            "pair": f"{from_c}->{to_c}", "rate": r["rate"],
            "observed_at": r.get("observed_at"),
        })
        return _d(r["amount"])

    # ------------------------------------------------------------------
    def summary(self, *, display_currency: str = "TWD",
                prices: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate every offline account into one display currency.

        ``prices`` maps instrument_id → current price (manual/verified
        quotes only — offline phase has no broker marks).
        """
        prices = prices or {}
        display = display_currency.upper()
        meta: dict[str, Any] = {}
        per_account: list[dict[str, Any]] = []
        totals = {"total_assets": Decimal(0), "cost_basis": Decimal(0),
                  "realized_pnl": Decimal(0), "unrealized_pnl": Decimal(0),
                  "dividends": Decimal(0), "cash": Decimal(0)}
        by_market: dict[str, Decimal] = {}
        by_currency: dict[str, Decimal] = {}

        for acct in self._accounts.list_accounts():
            aid = acct["account_id"]
            view = self._accounts.account_view(aid)
            a_total = Decimal(0)
            a_unreal = Decimal(0)
            a_real = Decimal(0)
            a_div = Decimal(0)
            a_cost = Decimal(0)
            a_cash = Decimal(0)

            for c in view["cash"]:
                amt = _d(c["amount"])
                conv = self._convert(amt, c["currency"], display, meta)
                if conv is not None:
                    a_cash += conv
                    a_total += conv
                    by_currency[c["currency"]] = \
                        by_currency.get(c["currency"], Decimal(0)) + amt
            for h in view["holdings"]:
                qty, cost = _d(h["quantity"]), _d(h["avg_cost"])
                basis = qty * cost
                cur = prices.get(h["instrument_id"])
                value = qty * _d(cur) if cur is not None else basis
                conv = self._convert(value, h["currency"], display, meta)
                cb = self._convert(basis, h["currency"], display, meta)
                if conv is not None:
                    a_total += conv
                    by_market[h["market"]] = \
                        by_market.get(h["market"], Decimal(0)) + conv
                    if cur is not None and cb is not None:
                        a_unreal += conv - cb
                if cb is not None:
                    a_cost += cb
            for t in view["transactions"]:
                if str(t.get("side")).lower() == "sell":
                    a_real += _d(t["quantity"]) * (
                        _d(t["price"]) - _d(t.get("avg_cost") or 0))
            for d in view["dividends"]:
                conv = self._convert(_d(d["amount"]), d["currency"],
                                     display, meta)
                if conv is not None:
                    a_div += conv

            totals["total_assets"] += a_total
            totals["cost_basis"] += a_cost
            totals["unrealized_pnl"] += a_unreal
            totals["realized_pnl"] += a_real
            totals["dividends"] += a_div
            totals["cash"] += a_cash
            per_account.append({
                "account_id": aid, "kind": acct["kind"],
                "broker_id": acct["broker_id"], "label": acct["label"],
                "source": acct["source"],
                "broker_confirmed": False,
                "total_assets": str(a_total), "cash": str(a_cash),
                "unrealized_pnl": str(a_unreal),
                "holdings": len(view["holdings"]),
            })

        total = totals["total_assets"]
        allocation = {}
        if total > 0:
            allocation = {
                "by_market": {m: str(v / total) for m, v in
                              by_market.items()},
                "cash_weight": str(totals["cash"] / total),
            }
        return {
            "ok": True,
            "display_currency": display,
            "total_assets": str(total),
            "cost_basis": str(totals["cost_basis"]),
            "realized_pnl": str(totals["realized_pnl"]),
            "unrealized_pnl": str(totals["unrealized_pnl"]),
            "dividends": str(totals["dividends"]),
            "cash": str(totals["cash"]),
            "by_market": {m: str(v) for m, v in by_market.items()},
            "currency_exposure": {c: str(v) for c, v in
                                  by_currency.items()},
            "allocation": allocation,
            "accounts": per_account,
            "fx_rates": meta.get("fx_rates", []),
            "fx_unavailable": meta.get("fx_unavailable", False),
            "missing_pairs": sorted(meta.get("missing_pairs", set())),
            "note": "offline aggregation — holdings are manual/imported, "
                    "never broker-confirmed",
        }
