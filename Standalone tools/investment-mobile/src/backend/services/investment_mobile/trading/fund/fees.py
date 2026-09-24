"""FundFeeEngine — multi-dimensional fee rules, no double deduction.

Rules are scoped by fund / share class / platform / effective dates /
holding period / currency, and support fixed, percent, holding-period
and tiered calculations. Management + custody fees are NAV-embedded —
they are reported separately and NEVER deducted again on top of NAV.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import FeeKind, FundFee


class FundFeeEngine:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-fees.jsonl"

    # ------------------------------------------------------------------
    def register(self, fee: FundFee) -> dict[str, Any]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(fee.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "fee": fee.to_dict()}

    def rules(
        self, fund_id: str | None = None, share_class_id: str | None = None,
    ) -> list[FundFee]:
        if not self._path.is_file():
            return []
        out: list[FundFee] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                fee = FundFee(
                    fee_id=row["fee_id"], fund_id=row["fund_id"],
                    share_class_id=row["share_class_id"], kind=row["kind"],
                    calc=row["calc"], rate=Decimal(row["rate"]),
                    currency=row.get("currency", ""),
                    platform=row.get("platform", ""),
                    min_holding_days=row.get("min_holding_days"),
                    tiers=row.get("tiers") or [],
                    effective_from=row.get("effective_from", ""),
                    effective_to=row.get("effective_to", ""),
                    source_id=row.get("source_id", "operator"),
                )
            except (json.JSONDecodeError, KeyError):
                continue
            if fund_id and fee.fund_id != fund_id:
                continue
            if share_class_id and fee.share_class_id != share_class_id:
                continue
            out.append(fee)
        return out

    # ------------------------------------------------------------------
    def transaction_fees(
        self,
        fund_id: str,
        share_class_id: str,
        kind_filter: tuple[str, ...],
        amount: Decimal | str | float,
        platform: str = "",
        trade_date: date | None = None,
        holding_days: int | None = None,
        currency: str = "",
    ) -> dict[str, Any]:
        """Investor-paid fees only — NAV-embedded kinds are excluded and
        reported separately so they are never deducted twice."""
        amount = Decimal(str(amount))
        trade_date = trade_date or date.today()
        charged: list[dict[str, Any]] = []
        embedded: list[dict[str, Any]] = []
        total = Decimal("0")
        for fee in self.rules(fund_id, share_class_id):
            if fee.nav_embedded:
                embedded.append({"kind": fee.kind, "rate": str(fee.rate),
                                 "note": "already reflected in NAV"})
                continue
            if kind_filter and fee.kind not in kind_filter:
                continue
            if not fee.applies(platform, trade_date, holding_days, currency):
                continue
            charge = fee.charge(amount, holding_days)
            total += charge
            charged.append({
                "fee_id": fee.fee_id, "kind": fee.kind, "calc": fee.calc,
                "rate": str(fee.rate), "charge": str(charge),
                "currency": fee.currency or currency,
            })
        return {
            "ok": True, "amount": str(amount),
            "investor_paid_total": str(total),
            "charged": charged, "nav_embedded": embedded,
        }

    def compare(
        self, fund_ids: list[tuple[str, str]],
        amount: Decimal | str | float, platform: str = "",
        holding_days: int | None = None,
    ) -> dict[str, Any]:
        """Fee comparison split: investor-paid vs NAV-embedded ongoing."""
        out = []
        for fund_id, share_class_id in fund_ids:
            res = self.transaction_fees(
                fund_id, share_class_id,
                (FeeKind.SUBSCRIPTION.value, FeeKind.REDEMPTION.value,
                 FeeKind.SHORT_TERM.value, FeeKind.PLATFORM.value,
                 FeeKind.FX.value, FeeKind.OTHER.value),
                amount, platform=platform, holding_days=holding_days,
            )
            ongoing = [
                f for f in self.rules(fund_id, share_class_id)
                if f.nav_embedded
            ]
            out.append({
                "fund_id": fund_id, "share_class_id": share_class_id,
                "investor_paid_total": res["investor_paid_total"],
                "charged": res["charged"],
                "ongoing_annual_pct": str(sum(
                    (f.rate for f in ongoing), Decimal("0"))),
            })
        return {"ok": True, "comparison": out}
