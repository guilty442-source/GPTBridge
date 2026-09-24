"""AI 投資分析 domain — 星澄 analysis surface.

星澄提供：市場趨勢、投組分析、策略研究、資產配置、市場風險、投資報告。
All model access goes through the governed AI channel
(``InvestmentAiConnections`` → xingcheng); this domain composes prompts
and records runs — it never touches a model directly.
"""

from __future__ import annotations

from ...domain.contract import DOMAIN_AI_ANALYSIS
from .base import BusinessDomain

_ANALYSIS_KINDS = {
    "investment_ai_trend": "market-trend",
    "investment_ai_portfolio_analysis": "portfolio-analysis",
    "investment_ai_strategy_research": "strategy-research",
    "investment_ai_allocation": "asset-allocation",
    "investment_ai_risk": "market-risk",
    "investment_ai_report": "investment-report",
    "investment_ai_research_search": "research-search",
}

_DEFAULT_PROMPTS = {
    "market-trend": "分析目前台股與美股市場趨勢",
    "portfolio-analysis": "分析目前投資組合配置與集中度風險",
    "strategy-research": "研究適合目前市場的交易策略",
    "asset-allocation": "分析全資產配置並提出再平衡建議",
    "market-risk": "分析目前市場風險與壓力情境",
    "investment-report": "產出本期投資報告",
    "research-search": "檢索金融研究資料",
}

# Qdrant canonical semantic index for financial research
# (shared-layer/migrations/134_trading_system.sql registers the collection).
RESEARCH_COLLECTION = "star-financial-research-v1"


class AiAnalysisDomain(BusinessDomain):
    domain_id = DOMAIN_AI_ANALYSIS
    label = "AI 投資分析"
    commands = frozenset(_ANALYSIS_KINDS)

    async def handle(self, command, payload, *, store, ai_connections):
        kind = _ANALYSIS_KINDS.get(str(command))
        if kind is None:
            raise PermissionError("PERMISSION_DENIED")
        prompt = str(payload.get("prompt") or _DEFAULT_PROMPTS[kind])
        if kind == "research-search":
            # Semantic retrieval is mediated through 星澄/RAG — this domain
            # never opens a direct Qdrant connection.
            result = await ai_connections.consult(
                f"[collection:{RESEARCH_COLLECTION}] {prompt}",
                "financial-research",
            )
            return {
                "ok": result.get("ok") is not False,
                "domain": self.domain_id,
                "kind": kind,
                "collection": RESEARCH_COLLECTION,
                "results": result,
            }
        result = await ai_connections.consult(prompt, f"investment-{kind}")
        if kind == "investment-report" and result.get("ok") is not False:
            report_id = store.record_report(
                kind, {"prompt": prompt, "analysis": result}
            )
            return {
                "ok": True,
                "domain": self.domain_id,
                "kind": kind,
                "report_id": report_id,
                "analysis": result,
            }
        return {
            "ok": result.get("ok") is not False,
            "domain": self.domain_id,
            "kind": kind,
            "analysis": result,
        }
