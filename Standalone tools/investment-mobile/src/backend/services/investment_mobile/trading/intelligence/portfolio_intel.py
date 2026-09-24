"""PortfolioIntelligence — unified cross-asset analysis.

Merges direct holdings (TW/US stock/ETF), fund positions (look-through)
and cash into one exposure view. Look-through fund holdings contribute
to *exposure* analysis but are never double-counted in total assets and
never treated as directly tradable shares.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..fund.engine import MutualFundEngine
from ..market.fx import CurrencyRateService
from ..portfolio_engine import PortfolioEngine
from .contracts import AnalysisTaskKind
from .router import ModelRouter


class PortfolioIntelligence:
    task_kind = AnalysisTaskKind.PORTFOLIO_RISK

    def __init__(self, portfolio: PortfolioEngine,
                 fund_engine: MutualFundEngine,
                 fx: CurrencyRateService,
                 router: ModelRouter,
                 accounts: Any | None = None) -> None:
        self._portfolio = portfolio
        self._fund = fund_engine
        self._fx = fx
        self._router = router
        self._accounts = accounts

    async def analyze(
        self, *, base_currency: str = "TWD",
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        positions = self._positions(account_ids)
        fund_positions = self._fund_positions(account_ids)
        cash = self._cash(account_ids)

        direct_value = sum(
            self._convert(Decimal(str(p.get("market_value") or 0)),
                          str(p.get("currency") or base_currency),
                          base_currency)
            for p in positions)
        fund_value = sum(
            self._convert(Decimal(str(p.get("market_value") or 0)),
                          str(p.get("currency") or base_currency),
                          base_currency)
            for p in fund_positions)
        cash_value = sum(
            self._convert(Decimal(str(c.get("amount") or 0)),
                          str(c.get("currency") or base_currency),
                          base_currency)
            for c in cash)
        total = direct_value + fund_value + cash_value

        look_through: dict[str, Decimal] = {}
        for fp in fund_positions:
            h = self._fund.exposure.holdings(str(fp.get("fund_id") or ""))
            mv = Decimal(str(fp.get("market_value") or 0))
            for holding in h.get("holdings", []):
                w = Decimal(str(holding.get("weight") or 0))
                hid = str(holding.get("holding_id") or "")
                look_through[hid] = look_through.get(hid, Decimal(0)) + mv * w

        exposure = dict(look_through)
        for p in positions:
            iid = str(p.get("instrument_id") or "")
            mv = self._convert(Decimal(str(p.get("market_value") or 0)),
                               str(p.get("currency") or base_currency),
                               base_currency)
            exposure[iid] = exposure.get(iid, Decimal(0)) + mv

        concentration = (
            {iid: float(v / total) for iid, v in exposure.items()}
            if total > 0 else {}
        )
        direct_ids = {str(p.get("instrument_id")) for p in positions}
        overlap_alerts = sorted(iid for iid in look_through
                                if iid in direct_ids)

        interp = await self._router.infer(
            self.task_kind,
            "分析整體資產配置、集中度與重疊曝險", fallback_text="")

        estimated = any(
            str(p.get("data_status") or "") in ("estimated", "missing")
            for p in positions + fund_positions
        )
        return {
            "ok": True,
            "degraded": interp.degraded,
            "estimated": estimated,
            "base_currency": base_currency,
            "total_assets": str(total),
            "direct_value": str(direct_value),
            "fund_value": str(fund_value),
            "cash_value": str(cash_value),
            "concentration": concentration,
            "look_through_components": {
                k: str(v) for k, v in look_through.items()},
            "overlap_alerts": overlap_alerts,
            "positions_count": len(positions),
            "fund_positions_count": len(fund_positions),
            "model_interpretation": interp.text,
            "note": "穿透持股僅作曝險分析，非可交易持股；總資產不重複計入",
        }

    # ------------------------------------------------------------------
    def _positions(self, account_ids: list[str] | None) -> list[dict]:
        if account_ids:
            out: list[dict] = []
            for a in account_ids:
                out.extend(self._portfolio.positions(a))
            return out
        return self._portfolio.positions()

    def _fund_positions(self, account_ids: list[str] | None) -> list[dict]:
        """Fund positions from settled transactions × latest published NAV."""
        accts = account_ids
        seen: set[tuple[str, str, str]] = set()
        for t in self._fund.transactions.settled():
            if accts and t.account_id not in accts:
                continue
            seen.add((t.account_id, t.fund_id, t.share_class_id))
        out: list[dict] = []
        for acct, fid, cls in sorted(seen):
            b = self._fund.cost_basis.basis(acct, fid, cls)
            units = Decimal(str(b.get("units") or 0))
            if units <= 0:
                continue
            latest = self._fund.nav.latest_published(fid, cls)
            nav_row = latest.get("nav") or {}
            nav = Decimal(str(nav_row.get("nav") or 0))
            out.append({
                "fund_id": fid, "share_class_id": cls,
                "units": str(units),
                "market_value": str(units * nav),
                "currency": nav_row.get("currency") or "TWD",
                "data_status": "estimated" if latest.get("stale") else "ok",
            })
        return out

    def _cash(self, account_ids: list[str] | None) -> list[dict]:
        if self._accounts is None:
            return []
        allowed = set(account_ids) if account_ids else None
        out: list[dict] = []
        for row in self._accounts.list_cash():
            if allowed and row.get("account_id") not in allowed:
                continue
            out.append({
                "amount": str(row.get("available") or 0),
                "currency": str(row.get("currency") or ""),
            })
        return out

    def _convert(self, amount: Decimal, src: str, dst: str) -> Decimal:
        if src == dst:
            return amount
        res = self._fx.convert(amount, src, dst)
        if isinstance(res, dict) and res.get("ok"):
            return Decimal(str(res.get("amount")))
        return amount  # no rate → keep nominal, marked by caller
