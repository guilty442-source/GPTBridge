"""AIStrategyResearchService — 星澄 research over backtest artifacts.

AI may analyze results, explain failures, and propose parameter ideas —
it can never modify a released strategy, start LIVE, lift risk limits,
or change capital. Proposals land as DRAFT versions only, routed through
backtest → validation → shadow → paper like everything else.
"""

from __future__ import annotations

from typing import Any

from ..intelligence.contracts import AnalysisTaskKind
from ..intelligence.router import ModelRouter
from .contracts import StrategyDefinition, StrategyStatus
from .registry import StrategyRegistry


class AIStrategyResearchService:
    def __init__(self, router: ModelRouter,
                 registry: StrategyRegistry) -> None:
        self._router = router
        self._registry = registry

    async def analyze_result(
        self, run_summary: dict[str, Any], *,
        question: str = "分析回測結果與失敗原因",
    ) -> dict[str, Any]:
        """Interpret a BacktestResult — model annotates, never edits."""
        prompt = (
            f"{question}\n"
            f"metrics={run_summary.get('metrics')}\n"
            f"costs={run_summary.get('cost_total')} "
            f"trades={run_summary.get('trade_count')}"
        )
        res = await self._router.infer(
            AnalysisTaskKind.DEEP_RESEARCH, prompt,
            fallback_text="",
            run_id=str(run_summary.get("run_id") or ""))
        return {
            "ok": True,
            "degraded": res.degraded,
            "interpretation": res.text,
            "kind": "MODEL_INTERPRETATION",
            "note": "AI 僅解讀；策略修改走 draft→backtest→validation→shadow→paper",
        }

    async def propose_draft(
        self, base_strategy_id: str,
        suggested_parameters: dict[str, Any],
        *, reasoning: str = "",
    ) -> dict[str, Any]:
        """AI proposals create NEW draft versions — never overwrite."""
        base = self._registry.get(base_strategy_id)
        if base is None:
            return {"ok": False, "error_code": "STRATEGY_NOT_FOUND"}
        new_def = StrategyDefinition(
            strategy_id=base["strategy_id"],
            strategy_name=base["strategy_name"],
            strategy_type=base["strategy_type"],
            market=base["market"],
            instrument_scope=list(base["instrument_scope"]),
            timeframe=base["timeframe"],
            parameters=dict(suggested_parameters),
            risk_profile=dict(base["risk_profile"]),
            version=int(base["version"]) + 1,
            status=StrategyStatus.DRAFT,
        )
        res = self._registry.register(new_def)
        if res.get("ok"):
            res["ai_proposal"] = {
                "reasoning": reasoning,
                "source": "xingcheng-research",
                "pipeline": "draft→backtest→validation→shadow→paper",
            }
        return res

    def boundary(self) -> dict[str, Any]:
        return {
            "ai_may": ["analyze results", "explain failures",
                       "propose parameter drafts",
                       "analyze cost/drawdown causes"],
            "ai_may_not": ["modify released strategies", "start LIVE",
                           "lift risk limits", "add capital",
                           "treat simulated returns as real"],
        }
