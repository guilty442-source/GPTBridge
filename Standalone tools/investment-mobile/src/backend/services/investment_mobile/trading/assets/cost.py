"""CostBasisEngine — lot-aware cost and PnL accounting.

Methods per account: ``average`` (TW convention) or ``fifo``
(US convention). Handles buys, partial sells, fund subscriptions/
redemptions, splits, stock dividends and reinvested distributions.

Rules honoured:

- The method is configurable per account and recorded on every result.
- Market-price moves NEVER rewrite cost basis — only corporate actions
  and new transactions do.
- Corrections replay in journal order (original first, then its
  correction chain) so the corrected basis is reproducible.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

COST_METHODS = frozenset({"average", "fifo"})


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class _Lot:
    __slots__ = ("qty", "cost")

    def __init__(self, qty: Decimal, cost: Decimal) -> None:
        self.qty, self.cost = qty, cost


class CostBasisEngine:
    def __init__(self, ledger: Any,
                 account_methods: dict[str, str] | None = None) -> None:
        self._ledger = ledger
        self._methods = dict(account_methods or {})

    def set_method(self, account_id: str, method: str) -> dict[str, Any]:
        if method not in COST_METHODS:
            return {"ok": False, "error_code": "COST_METHOD_UNKNOWN",
                    "methods": sorted(COST_METHODS)}
        self._methods[account_id] = method
        return {"ok": True, "account_id": account_id, "method": method}

    def method_for(self, account_id: str) -> str:
        return self._methods.get(account_id, "average")

    # ------------------------------------------------------------------
    def position_cost(self, account_id: str,
                      instrument_id: str) -> dict[str, Any]:
        """Replay transactions → lots → cost basis + realized PnL."""
        method = self.method_for(account_id)
        txns = self._effective_txns(account_id, instrument_id)
        lots: list[_Lot] = []
        realized = Decimal(0)
        invested = Decimal(0)      # cumulative net buy cost
        for t in txns:
            tt = t["transaction_type"]
            qty, price = _d(t.get("quantity")), _d(t.get("price"))
            fee = _d(t.get("fees") or t.get("fee"))
            tax = _d(t.get("taxes") or t.get("tax"))
            if tt in ("buy", "subscribe"):
                cost = qty * price + fee + tax
                invested += cost
                self._add_lot(lots, qty, cost, method)
            elif tt in ("sell", "redeem"):
                proceeds = qty * price - fee - tax
                cogs = self._remove_lots(lots, qty, method)
                realized += proceeds - cogs
            elif tt == "split":
                ratio = _d(t.get("price") or "1") or Decimal(1)
                for lot in lots:
                    lot.qty *= ratio   # cost per share dilutes
            elif tt == "stock_dividend":
                # stock dividend adds shares, total cost unchanged
                if lots:
                    lots[-1].qty += qty
                else:
                    lots.append(_Lot(qty, Decimal(0)))
            elif tt == "reinvest":
                # reinvested distribution = a buy funded by income
                cost = qty * price
                invested += cost
                self._add_lot(lots, qty, cost, method)
        qty_total = sum(l.qty for l in lots)
        cost_total = sum(l.cost for l in lots)
        avg = (cost_total / qty_total) if qty_total > 0 else Decimal(0)
        return {
            "ok": True, "account_id": account_id,
            "instrument_id": instrument_id, "method": method,
            "quantity": str(qty_total),
            "cost_basis": str(cost_total),
            "average_cost": str(avg),
            "invested_capital": str(invested),
            "realized_pnl": str(realized),
            "lots": [{"quantity": str(l.qty), "cost": str(l.cost)}
                     for l in lots],
        }

    def _effective_txns(self, account_id: str,
                        instrument_id: str) -> list[dict[str, Any]]:
        """Originals in journal order with their latest correction
        values applied — the original row is kept, never mutated."""
        out: list[dict[str, Any]] = []
        for t in self._ledger.list(account_id, instrument_id):
            eff = dict(t)
            if t.get("corrections"):
                eff.update({k: v for k, v in t["corrections"][-1].items()
                            if k not in ("corrects", "correction_reason",
                                         "corrected_at", "txn_id",
                                         "transaction_id", "entry_id")})
            eff.setdefault("transaction_type",
                           t.get("side") or "buy")
            out.append(eff)
        return out

    def _add_lot(self, lots: list[_Lot], qty: Decimal, cost: Decimal,
                 method: str) -> None:
        if method == "average" and lots:
            total_q = sum(l.qty for l in lots) + qty
            total_c = sum(l.cost for l in lots) + cost
            lots.clear()
            lots.append(_Lot(total_q, total_c))
        else:
            lots.append(_Lot(qty, cost))

    def _remove_lots(self, lots: list[_Lot], qty: Decimal,
                     method: str) -> Decimal:
        """Return cost-of-goods-sold for ``qty`` shares/units."""
        if method == "average" and lots:
            lot = lots[0]
            if lot.qty <= 0:
                return Decimal(0)
            avg = lot.cost / lot.qty
            take = min(qty, lot.qty)
            cogs = avg * take
            lot.qty -= take
            lot.cost -= cogs
            if lot.qty <= 0:
                lots.pop(0)
            return cogs
        remaining, cogs = qty, Decimal(0)
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot.qty)
            unit = lot.cost / lot.qty if lot.qty > 0 else Decimal(0)
            cogs += unit * take
            lot.qty -= take
            lot.cost -= unit * take
            remaining -= take
            if lot.qty <= 0:
                lots.pop(0)
        return cogs
