"""星澄 asset-analysis surface + recommendation generator.

PortfolioIntelligenceService: governed read views the model consumes —
every result carries data sources, valuation dates, analysis time,
completeness and the engine version. The model is expected to cite the
deterministic numbers; it must never invent asset figures.

PortfolioRecommendationService: deterministic candidate generation
(drift breach → rebalance suggestion with estimated costs); AI may
annotate but cannot turn a suggestion into an order.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

ENGINE_VERSION = "assets/v1"


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PortfolioIntelligenceService:
    """Read-only analysis surface for 星澄 over the asset engines."""

    def __init__(self, valuation: Any, allocation: Any,
                 exposure: Any, risk: Any, income: Any,
                 performance: Any) -> None:
        self._valuation = valuation
        self._allocation = allocation
        self._exposure = exposure
        self._risk = risk
        self._income = income
        self._performance = performance

    def _envelope(self, data: dict[str, Any],
                  sources: list[str]) -> dict[str, Any]:
        return {
            **data,
            "analysis_meta": {
                "engine_version": ENGINE_VERSION,
                "data_sources": sources,
                "valuation_times": data.get("valuation_times", {}),
                "analyzed_at": time.time(),
                "completeness": ("partial" if data.get("stale_data")
                                 or data.get("reference_only")
                                 else "complete"),
            },
        }

    # ------------------------------------------------------------------
    def analyze_total(self, valuation: dict[str, Any]) -> dict[str, Any]:
        return self._envelope(valuation,
                              ["offline-accounts", "fx-rates"])

    def analyze_account(self, valuation: dict[str, Any],
                        account_id: str) -> dict[str, Any]:
        positions = [p for p in valuation.get("positions", [])
                     if p["account_id"] == account_id]
        return self._envelope({
            "ok": True, "account_id": account_id,
            "positions": positions,
            "total": valuation.get("per_account", {}).get(
                account_id, "0"),
        }, ["offline-accounts"])

    def analyze_risk(self, valuation: dict[str, Any],
                     valuations: list[dict[str, Any]]) -> dict[str, Any]:
        return self._envelope(
            self._risk.report(valuation, valuations),
            ["snapshots", "valuation"])

    def analyze_overlap(self, valuation: dict[str, Any]) -> dict[str, Any]:
        return self._envelope(
            self._exposure.analyze(valuation),
            ["lookthrough-baskets", "valuation"])

    def analyze_cashflow(self, *, window: str = "month") -> dict[str, Any]:
        return self._envelope(
            self._income.cashflow(window=window), ["income-journal"])


class PortfolioRecommendationService:
    """Deterministic rebalancing candidates — advisory records only."""

    def __init__(self, allocation: Any, cost_estimator: Any | None = None
                 ) -> None:
        self._allocation = allocation
        self._cost = cost_estimator

    def recommend(self, valuation: dict[str, Any],
                  tags: dict[str, dict[str, str]] | None = None
                  ) -> dict[str, Any]:
        analysis = self._allocation.analyze(valuation, tags)
        if not analysis.get("ok"):
            return analysis
        suggestions = []
        for b in analysis["breaches"]:
            est = None
            if self._cost is not None:
                try:
                    est = self._cost.estimate(
                        {"market": "", "side": "sell",
                         "notional": str(abs(_d(b["drift"])) *
                                         _d(analysis["total_assets"]))})
                except Exception:
                    est = None
            suggestions.append({
                "kind": "rebalance",
                "dimension": b["dimension"], "key": b["key"],
                "current": b["actual"], "target": b.get("target"),
                "action": ("reduce" if _d(b["drift"]) > 0
                           else "increase"),
                "estimated_cost": est,
                "risk": "selling concentrated positions may realize "
                        "gains/taxes; partial reduction preferred",
            })
        if not suggestions:
            suggestions.append({"kind": "maintain",
                                "rationale": "within target bands"})
        return {"ok": True,
                "analysis": "deterministic — drift vs targets",
                "candidates": suggestions,
                "advisory": True,
                "note": "candidates never become orders; execution "
                        "requires the governed trading pipeline"}
