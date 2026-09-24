"""FundTransactionService — explicit status machine.

SUBMITTED is not SETTLED: redemption proceeds never touch account cash
before SETTLEMENT, and subscription units only exist once the trade is
PRICED and SETTLED with a confirmed NAV. Transitions follow the legal
state graph; platform capabilities may restrict further.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import (
    FundTransaction, FundTransactionType, FundTxnStatus,
)

_BUY_KINDS = frozenset({
    FundTransactionType.SUBSCRIBE.value, FundTransactionType.ADD.value,
    FundTransactionType.RECURRING.value, FundTransactionType.REINVEST.value,
    FundTransactionType.SWITCH.value,   # switch-in leg
})
_SELL_KINDS = frozenset({
    FundTransactionType.REDEEM.value,
    FundTransactionType.PARTIAL_REDEEM.value,
})


class FundTransactionService:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-transactions.jsonl"

    # ------------------------------------------------------------------
    def create(self, txn: FundTransaction) -> dict[str, Any]:
        if txn.transaction_type not in {
            t.value for t in FundTransactionType
        }:
            return {"ok": False, "error_code": "TXN_TYPE_UNKNOWN"}
        self._append(txn)
        return {"ok": True, "transaction": txn.to_dict()}

    def import_settled(self, txn: FundTransaction) -> dict[str, Any]:
        """Manual import of an already-confirmed platform record.

        Allowed only with confirmed NAV + units — it records history,
        it never creates live orders."""
        if txn.confirmed_nav is None or txn.units <= 0:
            return {"ok": False, "error_code": "IMPORT_REQUIRES_CONFIRMED_DATA"}
        txn.status = FundTxnStatus.SETTLED.value
        self._append(txn)
        return {"ok": True, "transaction": txn.to_dict()}

    def transition(
        self, transaction_id: str, target: str, **patch: Any,
    ) -> dict[str, Any]:
        txns = self._all()
        txn = next(
            (t for t in txns if t.transaction_id == transaction_id), None)
        if txn is None:
            return {"ok": False, "error_code": "TXN_UNKNOWN"}
        if not txn.can_transition(target):
            return {
                "ok": False, "error_code": "ILLEGAL_TRANSITION",
                "from": txn.status, "to": target,
            }
        if target == FundTxnStatus.PRICED.value:
            nav = patch.get("confirmed_nav")
            if nav is None or Decimal(str(nav)) <= 0:
                return {"ok": False, "error_code": "PRICED_REQUIRES_NAV"}
            txn.confirmed_nav = Decimal(str(nav))
            txn.pricing_date = patch.get("pricing_date") or date.today()
            if not txn.units and txn.confirmed_nav:
                txn.units = (txn.amount - txn.fees) / txn.confirmed_nav
        if target == FundTxnStatus.SETTLED.value:
            if txn.transaction_type in _BUY_KINDS and txn.units <= 0:
                return {"ok": False, "error_code": "SETTLE_REQUIRES_UNITS"}
            if txn.confirmed_nav is None and txn.transaction_type in (
                _BUY_KINDS | _SELL_KINDS
            ):
                return {"ok": False, "error_code": "SETTLE_REQUIRES_NAV"}
            txn.settlement_date = (
                patch.get("settlement_date") or date.today())
        txn.status = target
        for k, v in patch.items():
            if hasattr(txn, k) and v is not None:
                setattr(txn, k, v)
        self._rewrite(txns)
        return {"ok": True, "transaction": txn.to_dict()}

    # ------------------------------------------------------------------
    def pending_settlement(self, account_id: str | None = None) -> list[dict[str, Any]]:
        """Cash proceeds that must NOT be spendable yet."""
        return [
            t.to_dict() for t in self._all()
            if t.transaction_type in _SELL_KINDS
            and t.status in (
                FundTxnStatus.PRICED.value,
                FundTxnStatus.SETTLEMENT_PENDING.value,
            )
            and (account_id is None or t.account_id == account_id)
        ]

    def settled(self, fund_id: str | None = None,
                account_id: str | None = None) -> list[FundTransaction]:
        return [
            t for t in self._all()
            if t.credited
            and (fund_id is None or t.fund_id == fund_id)
            and (account_id is None or t.account_id == account_id)
        ]

    def list(self, account_id: str | None = None) -> list[dict[str, Any]]:
        return [
            t.to_dict() for t in self._all()
            if account_id is None or t.account_id == account_id
        ]

    # ------------------------------------------------------------------
    def _all(self) -> list[FundTransaction]:
        if not self._path.is_file():
            return []
        out: list[FundTransaction] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out.append(FundTransaction(
                    transaction_id=row["transaction_id"],
                    account_id=row["account_id"], fund_id=row["fund_id"],
                    share_class_id=row["share_class_id"],
                    transaction_type=row["transaction_type"],
                    amount=row["amount"], units=row.get("units", "0"),
                    confirmed_nav=row.get("confirmed_nav"),
                    currency=row.get("currency", ""),
                    fees=row.get("fees", "0"), status=row["status"],
                    application_date=row.get("application_date"),
                    pricing_date=row.get("pricing_date"),
                    confirmation_date=row.get("confirmation_date"),
                    settlement_date=row.get("settlement_date"),
                    source_id=row.get("source_id", "operator"),
                    note=row.get("note", ""),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    def _append(self, txn: FundTransaction) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(txn.to_dict(), ensure_ascii=False) + "\n")

    def _rewrite(self, txns: list[FundTransaction]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text("\n".join(
            json.dumps(t.to_dict(), ensure_ascii=False) for t in txns
        ) + ("\n" if txns else ""), encoding="utf-8")
        tmp.replace(self._path)
