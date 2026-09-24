"""MutualFundIntelligence — fund analysis over the fund engine + model.

NAV-dated analysis only: funds never use realtime stock-price logic.
Suggestions map to SUBSCRIBE/ADD/HOLD/REDUCE/REDEEM/SWITCH and require
verified NAV basis, fee rules, holding-period context and allocation
targets — none of which the model may invent.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..fund.engine import MutualFundEngine
from .contracts import EvidenceKind, AnalysisTaskKind
from .router import ModelRouter


class MutualFundIntelligence:
    task_kind = AnalysisTaskKind.FUND

    def __init__(self, fund_engine: MutualFundEngine,
                 router: ModelRouter) -> None:
        self._fund = fund_engine
        self._router = router

    async def analyze(
        self, fund_id: str, share_class_id: str,
        *, account_id: str = "",
        target_weight: float | None = None,
    ) -> dict[str, Any]:
        latest = self._fund.nav.latest_published(fund_id, share_class_id)
        perf = self._fund.performance.all_periods(fund_id, share_class_id)
        risk = self._fund.performance.risk_metrics(fund_id, share_class_id)
        holdings = self._fund.exposure.holdings(fund_id)
        fees = self._fund.fees.rules(fund_id, share_class_id)
        dists = self._fund.distributions.totals(share_class_id)
        basis = (self._fund.cost_basis.basis(account_id, fund_id,
                                             share_class_id)
                 if account_id else None)

        findings = {
            "nav_basis": latest,
            "performance": perf.get("periods", {}),
            "risk_metrics": risk if risk.get("ok") else None,
            "holdings_count": len(holdings.get("holdings", [])),
            "holdings_as_of": holdings.get("as_of_date"),
            "coverage_ratio": holdings.get("coverage_ratio"),
            "fee_rules": len(fees),
            "distributions": dists,
            "position": basis,
            "target_weight": target_weight,
        }
        missing = []
        if not latest.get("ok"):
            missing.append("published_nav")
        if not perf.get("ok"):
            missing.append("performance_history")
        if not holdings.get("holdings"):
            missing.append("holdings_disclosure")

        interp = await self._router.infer(
            self.task_kind,
            f"基金 {fund_id}/{share_class_id} 分析：績效/風險/費用/配息/持股",
            fallback_text="",
        )
        suggestion = self._suggest(
            latest, perf, risk, basis, target_weight)

        return {
            "ok": True,
            "degraded": interp.degraded or bool(missing),
            "fund_id": fund_id, "share_class_id": share_class_id,
            "findings": findings,
            "suggestion": suggestion,
            "model_interpretation": interp.text,
            "missing_data": missing,
            "note": "淨值為公告值，非即時成交價；建議非交易指令",
        }

    # ------------------------------------------------------------------
    def _suggest(
        self, latest: dict, perf: dict, risk: dict,
        basis: dict | None, target_weight: float | None,
    ) -> dict[str, Any]:
        """Deterministic suggestion skeleton — model may only annotate."""
        rec = "HOLD"
        reasons: list[str] = []
        if not latest.get("ok"):
            return {"recommendation_type": "HOLD",
                    "reasoning": "無已公告淨值，無法評估",
                    "nav_date": None}
        if basis and target_weight is not None and basis.get("units"):
            # simple deviation rule — deterministic
            cur = float(basis.get("weight") or 0)
            if cur > target_weight * 1.2:
                rec, reasons = "REDUCE", ["配置超出目標上限"]
            elif cur < target_weight * 0.8:
                rec, reasons = "ADD", ["配置低於目標下限"]
        return {
            "recommendation_type": rec,
            "reasoning": "；".join(reasons) or "維持現況",
            "nav_date": latest.get("nav", {}).get("nav_date")
            if isinstance(latest.get("nav"), dict) else None,
            "nav_basis": latest.get("nav", {}).get("nav")
            if isinstance(latest.get("nav"), dict) else None,
            "stale": bool(latest.get("stale")),
        }
