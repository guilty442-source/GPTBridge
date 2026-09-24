"""InvestmentTransactionLedger — typed, append-only transaction history.

Types: buy | sell | subscribe | redeem | switch | fee | tax | dividend |
fund_distribution | reinvest | remittance | fx_exchange | adjustment.

Corrections never overwrite: a ``correct`` entry links to the original
``transaction_id`` and carries reason + timestamp; readers see the
original plus its correction chain. Data source tag flows through every
row so manual vs imported vs simulated data stay distinguishable.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from ..offline.accounts import OfflineAccountService

TRANSACTION_TYPES = frozenset({
    "buy", "sell", "subscribe", "redeem", "switch", "fee", "tax",
    "dividend", "fund_distribution", "reinvest", "remittance",
    "fx_exchange", "adjustment", "split", "stock_dividend",
})


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class InvestmentTransactionLedger:
    def __init__(self, offline: OfflineAccountService) -> None:
        self._offline = offline
        self._corrections: dict[str, list[dict[str, Any]]] = {}
        self._rebuild_corrections()

    def _rebuild_corrections(self) -> None:
        self._corrections.clear()
        for acct in self._offline.list_accounts():
            for t in self._offline.transactions(acct["account_id"]):
                if t.get("corrects"):
                    self._corrections.setdefault(
                        str(t["corrects"]), []).append(t)

    # ------------------------------------------------------------------
    def record(self, account_id: str, txn: dict[str, Any],
               *, source: str = "MANUAL") -> dict[str, Any]:
        ttype = str(txn.get("transaction_type") or "").lower()
        if ttype not in TRANSACTION_TYPES:
            return {"ok": False, "error_code": "TRANSACTION_TYPE_UNKNOWN",
                    "types": sorted(TRANSACTION_TYPES)}
        qty, price = _d(txn.get("quantity")), _d(txn.get("price"))
        gross = qty * price
        fee, tax = _d(txn.get("fee")), _d(txn.get("tax"))
        net = gross + fee + tax if ttype in ("buy", "subscribe",
                                             "reinvest") \
            else gross - fee - tax
        row = dict(txn)
        row.update({
            "transaction_type": ttype,
            "transaction_date": txn.get("transaction_date")
                                or time.time(),
            "settlement_date": txn.get("settlement_date"),
            "quantity": str(qty), "price": str(price),
            "gross_amount": str(gross),
            "fees": str(fee), "taxes": str(tax),
            "net_amount": str(net),
            "source_id": str(txn.get("source_id") or source),
        })
        r = self._offline.add_transaction(account_id, row,
                                          source=source)
        if r.get("ok"):
            self._rebuild_corrections()
            r["transaction"]["transaction_id"] = r["transaction"][
                "txn_id"]
        return r

    def correct(self, transaction_id: str, fields: dict[str, Any], *,
                reason: str, source: str = "MANUAL") -> dict[str, Any]:
        """Append a correction linked to the original — never overwrite."""
        orig = self.get(transaction_id)
        if orig is None:
            return {"ok": False,
                    "error_code": "TRANSACTION_NOT_FOUND"}
        merged = dict(orig)
        merged.pop("txn_id", None)
        merged.pop("entry_id", None)
        merged.pop("op", None)
        merged.update({k: v for k, v in fields.items()})
        merged["corrects"] = transaction_id
        merged["correction_reason"] = str(reason)
        merged["corrected_at"] = time.time()
        r = self.record(orig["account_id"], merged, source=source)
        if r.get("ok"):
            r["corrected"] = transaction_id
        return r

    # ------------------------------------------------------------------
    def get(self, transaction_id: str) -> dict[str, Any] | None:
        for acct in self._offline.list_accounts():
            for t in self._offline.transactions(acct["account_id"]):
                if t.get("txn_id") == transaction_id:
                    return t
        return None

    def list(self, account_id: str | None = None,
             instrument_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for acct in self._offline.list_accounts():
            aid = acct["account_id"]
            if account_id and aid != account_id:
                continue
            for t in self._offline.transactions(aid):
                if instrument_id and t.get("instrument_id") \
                        != instrument_id:
                    continue
                tid = t.get("txn_id")
                t = dict(t)
                t["transaction_id"] = tid
                t["corrections"] = [dict(c) for c in
                                    self._corrections.get(tid, [])]
                t["superseded"] = bool(t["corrections"])
                out.append(t)
        return out
