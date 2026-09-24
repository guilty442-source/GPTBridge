"""FundCostBasisService — average-cost basis over settled transactions.

Cost method: weighted average cost (per fund+share-class+account).
Subscriptions add (net amount incl. fee as cost, units at confirmed
NAV); partial redemptions realize P&L pro-rata; reinvested
distributions add units and cost; cash distributions accumulate
separately and are never double-counted into both income and value.
Confirmed platform data always wins over estimates.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .contracts import FundTransaction, FundTransactionType, FundTxnStatus
from .nav import FundNAVService
from .transactions import FundTransactionService

_BUY = frozenset({
    FundTransactionType.SUBSCRIBE.value, FundTransactionType.ADD.value,
    FundTransactionType.RECURRING.value, FundTransactionType.REINVEST.value,
})
_SELL = frozenset({
    FundTransactionType.REDEEM.value,
    FundTransactionType.PARTIAL_REDEEM.value,
})
_DIST_CASH = frozenset({FundTransactionType.DISTRIBUTION.value})


class FundCostBasisService:
    METHOD = "weighted_average"

    def __init__(
        self, transactions: FundTransactionService, nav: FundNAVService,
    ) -> None:
        self._txns = transactions
        self._nav = nav

    # ------------------------------------------------------------------
    def basis(
        self, account_id: str, fund_id: str, share_class_id: str,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        units = Decimal("0")
        cost = Decimal("0")               # invested principal incl. fees
        realized = Decimal("0")
        cash_dividends = Decimal("0")
        for t in self._txns.settled(fund_id=fund_id, account_id=account_id):
            if t.share_class_id != share_class_id:
                continue
            if t.transaction_type in _BUY:
                units += t.units
                cost += t.amount
            elif t.transaction_type in _SELL:
                if units <= 0:
                    continue
                avg = cost / units
                proceeds = t.amount - t.fees
                realized += proceeds - avg * t.units
                cost -= avg * t.units
                units -= t.units
            elif t.transaction_type in _DIST_CASH:
                cash_dividends += t.amount

        nav = self._nav.latest(fund_id, share_class_id, as_of=as_of)
        market_value = (
            units * nav.nav if nav is not None else None
        )
        unrealized = (market_value - cost) if market_value is not None else None
        total_pnl = (
            (unrealized or Decimal("0")) + realized + cash_dividends
        )
        return {
            "ok": True,
            "method": self.METHOD,
            "account_id": account_id, "fund_id": fund_id,
            "share_class_id": share_class_id,
            "units": str(units),
            "invested_principal": str(cost),
            "avg_cost_per_unit": str(cost / units) if units else None,
            "market_value": str(market_value) if market_value else None,
            "nav_date": nav.nav_date.isoformat() if nav else None,
            "realized_pnl": str(realized),
            "unrealized_pnl": (
                str(unrealized) if unrealized is not None else None
            ),
            "cash_dividends_received": str(cash_dividends),
            "total_pnl_incl_dividends": str(total_pnl),
        }
