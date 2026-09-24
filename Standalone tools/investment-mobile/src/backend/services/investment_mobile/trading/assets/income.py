"""InvestmentIncomeService — dividends & fund distributions.

Every income record: instrument, account, ex-date, pay-date, original
currency, gross amount, withholding, net received, source. Fund
distributions keep the披露 classification: ``income`` | ``principal`` |
``unconfirmed`` — a distribution yield alone is never treated as
performance evidence.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

INCOME_TYPES = frozenset({
    "tw_dividend", "tw_etf_distribution", "us_dividend",
    "us_etf_distribution", "fund_distribution",
})
FUND_DIST_CLASS = frozenset({"income", "principal", "unconfirmed"})


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class InvestmentIncomeService:
    def __init__(self, offline: Any) -> None:
        self._offline = offline

    # ------------------------------------------------------------------
    def record(self, account_id: str, item: dict[str, Any],
               *, source: str = "MANUAL") -> dict[str, Any]:
        itype = str(item.get("income_type") or "").lower()
        if itype not in INCOME_TYPES:
            return {"ok": False, "error_code": "INCOME_TYPE_UNKNOWN",
                    "types": sorted(INCOME_TYPES)}
        gross = _d(item.get("gross_amount") or item.get("amount"))
        withholding = _d(item.get("withholding"))
        row = dict(item)
        row.update({
            "income_type": itype,
            "gross_amount": str(gross),
            "withholding": str(withholding),
            "net_amount": str(gross - withholding),
            "amount": str(gross - withholding),
            "ex_date": item.get("ex_date"),
            "pay_date": item.get("paid_at") or item.get("pay_date")
                       or time.time(),
            "distribution_class": (
                str(item.get("distribution_class")).lower()
                if itype == "fund_distribution" else ""),
        })
        if itype == "fund_distribution" and \
                row["distribution_class"] not in FUND_DIST_CLASS:
            row["distribution_class"] = "unconfirmed"
        # income events are idempotent — the same (instrument, ex-date,
        # gross) is never double-counted as income AND as cash
        ident = (str(item.get("instrument_id") or ""),
                 str(row.get("ex_date") or ""),
                 str(row["gross_amount"]), itype)
        for prev in self._offline.dividends(account_id):
            if (str(prev.get("instrument_id") or ""),
                    str(prev.get("ex_date") or ""),
                    str(prev.get("gross_amount") or prev.get("amount")),
                    str(prev.get("income_type") or "")) == ident:
                return {"ok": False, "error_code": "INCOME_DUPLICATE",
                        "dividend_id": prev.get("dividend_id")}
        return self._offline.add_dividend(account_id, row,
                                          source=source)

    # ------------------------------------------------------------------
    def list(self, account_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for acct in self._offline.list_accounts():
            aid = acct["account_id"]
            if account_id and aid != account_id:
                continue
            out.extend(self._offline.dividends(aid))
        return out

    def cashflow(self, *, window: str = "all") -> dict[str, Any]:
        days = {"month": 30, "quarter": 91, "year": 365,
                "all": 10 ** 9}.get(window)
        if days is None:
            return {"ok": False, "error_code": "WINDOW_UNKNOWN"}
        cutoff = time.time() - days * 86400
        by_currency: dict[str, Decimal] = {}
        by_type: dict[str, Decimal] = {}
        count = 0
        for row in self.list():
            if float(row.get("paid_at") or row.get("pay_date")
                     or row.get("at") or 0) < cutoff:
                continue
            amt = _d(row.get("amount"))
            by_currency[row.get("currency", "")] = \
                by_currency.get(row.get("currency", ""), Decimal(0)) + amt
            by_type[row.get("income_type", "unknown")] = \
                by_type.get(row.get("income_type", "unknown"),
                            Decimal(0)) + amt
            count += 1
        return {"ok": True, "window": window, "count": count,
                "by_currency": {k: str(v) for k, v in
                                by_currency.items()},
                "by_type": {k: str(v) for k, v in by_type.items()}}

    def fund_distribution_breakdown(self) -> dict[str, Any]:
        out = {"income": Decimal(0), "principal": Decimal(0),
               "unconfirmed": Decimal(0)}
        for row in self.list():
            if row.get("income_type") != "fund_distribution":
                continue
            cls = row.get("distribution_class") or "unconfirmed"
            out[cls] = out.get(cls, Decimal(0)) + _d(row.get("amount"))
        return {"ok": True,
                "breakdown": {k: str(v) for k, v in out.items()},
                "note": "yield alone is not performance evidence"}
