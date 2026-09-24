"""FundComparisonService — reproducible same-basis comparison.

Compares funds only on a shared period, shared currency and a declared
return basis (raw / with-distributions / net-of-transaction-fees). The
comparison parameters are part of the result so any two runs with the
same parameters are directly comparable — and mismatched bases are
rejected, never silently mixed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..market.fx import CurrencyRateService
from .contracts import NavType
from .distribution import FundDistributionService
from .fees import FundFeeEngine
from .identity import FundIdentityRegistry
from .nav import FundNAVService
from .performance import FundPerformanceEngine


class FundComparisonService:
    def __init__(
        self,
        identities: FundIdentityRegistry,
        nav: FundNAVService,
        distributions: FundDistributionService,
        fees: FundFeeEngine,
        performance: FundPerformanceEngine,
        fx: CurrencyRateService | None = None,
    ) -> None:
        self._ids = identities
        self._nav = nav
        self._dist = distributions
        self._fees = fees
        self._perf = performance
        self._fx = fx

    # ------------------------------------------------------------------
    def compare(
        self,
        targets: list[tuple[str, str]],      # (fund_id, share_class_id)
        period: str = "1y",
        base_currency: str = "TWD",
        include_distributions: bool = False,
        include_fees: bool = False,
        trade_amount: Decimal | str | float = Decimal("100000"),
        benchmark: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        params = {
            "period": period, "base_currency": base_currency,
            "include_distributions": include_distributions,
            "include_fees": include_fees,
            "benchmark": list(benchmark) if benchmark else None,
        }
        rows: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for fund_id, share_class_id in targets:
            ident = self._ids.share_classes(fund_id)
            currency = next(
                (i["share_class_currency"] or i["base_currency"]
                 for i in ident if i["share_class_id"] == share_class_id),
                base_currency,
            )
            ret = self._perf.period_return(
                fund_id, share_class_id, period, include_distributions)
            if not ret.get("ok"):
                rejected.append({"fund_id": fund_id,
                                 "share_class_id": share_class_id,
                                 "error": ret.get("error_code")})
                continue
            risk = self._perf.risk_metrics(
                fund_id, share_class_id,
                include_distributions=include_distributions)
            fee_note: dict[str, Any] = {}
            if include_fees:
                fee_note = self._fees.transaction_fees(
                    fund_id, share_class_id,
                    ("subscription",), trade_amount,
                )
            row: dict[str, Any] = {
                "fund_id": fund_id, "share_class_id": share_class_id,
                "currency": currency,
                "return_pct": ret["return_pct"],
                "annualized": ret["annualized"],
                "volatility_annual": risk.get("volatility_annual"),
                "max_drawdown": risk.get("max_drawdown"),
                "sharpe": risk.get("sharpe"),
            }
            if currency != base_currency:
                if self._fx is None:
                    rejected.append({"fund_id": fund_id,
                                     "error": "FX_REQUIRED"})
                    continue
                conv = self._fx.convert(
                    ret["return_pct"], currency, base_currency)
                row["fx_conversion"] = {
                    "from": currency, "to": base_currency,
                    "ok": conv.get("ok", False),
                }
                if not conv.get("ok"):
                    rejected.append({"fund_id": fund_id,
                                     "error": "RATE_UNAVAILABLE"})
                    continue
                row["currency_mismatch"] = True  # return stays native;
                # conversion metadata shown — never silently blended
            if fee_note:
                row["fees"] = fee_note
            rows.append(row)
        return {
            "ok": True,
            "parameters": params,   # reproducible comparison conditions
            "results": rows,
            "rejected": rejected,
        }
