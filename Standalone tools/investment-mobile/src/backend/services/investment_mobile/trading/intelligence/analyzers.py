"""Market/intelligence analyzers — deterministic math + model interpretation.

Structure: the analyzer computes IndicatorSet + gathers evidence, then
(optionally) asks the routed model for interpretation. Findings always
carry both the computed block and the interpretation block, labeled.
"""

from __future__ import annotations

from typing import Any

from ..market.engine import MarketDataEngine
from .contracts import (
    AnalysisEvidence, AnalysisTaskKind, EvidenceKind,
)
from .indicators import IndicatorSet
from .router import ModelRouter


class _BaseIntelligence:
    """Shared machinery: candles → indicators → evidence → interpretation."""

    task_kind = AnalysisTaskKind.QUICK_MARKET
    market = ""

    def __init__(self, market_engine: MarketDataEngine,
                 router: ModelRouter, candles: Any = None) -> None:
        self._market = market_engine
        self._router = router
        self._candles = candles          # CandleStore (dataclass rows)

    async def analyze(
        self, instrument_id: str, *,
        benchmark_id: str | None = None,
        interpretation_prompt: str = "",
    ) -> dict[str, Any]:
        candles = (
            self._candles.candles(instrument_id, "1d")[-300:]
            if self._candles is not None else []
        )
        evidence: list[AnalysisEvidence] = []
        findings: dict[str, Any] = {"instrument_id": instrument_id}
        missing: list[str] = []

        if not candles:
            missing.append("market_candles")
            return {
                "ok": True, "degraded": True,
                "instrument_id": instrument_id,
                "findings": findings, "missing_data": missing,
                "evidence": [], "note": "行情資料不足，分析降級",
            }

        closes = [float(c.close) for c in candles]
        volumes = [float(c.volume or 0) for c in candles]
        bench = None
        if benchmark_id and self._candles is not None:
            bench_candles = self._candles.candles(benchmark_id, "1d")[-300:]
            bench = [float(c.close) for c in bench_candles]
            if not bench:
                missing.append("benchmark_candles")

        ind = IndicatorSet(closes, volumes, bench)
        findings["indicators"] = ind.to_dict()
        evidence.append(AnalysisEvidence(
            kind=EvidenceKind.CALCULATED_RESULT,
            claim="技術指標由確定性模組計算",
            value=ind.to_dict(), computation="IndicatorSet",
            data_timestamp=candles[-1].candle_end.isoformat(),
            source_id=str(candles[-1].source_id or "unknown"),
        ))

        quote = self._market.latest_quote(instrument_id)
        if quote:
            findings["latest_quote"] = {
                "price": str(quote.get("last_price") or ""),
                "data_status": quote.get("data_status"),
                "timestamp": quote.get("received_timestamp"),
            }
            evidence.append(AnalysisEvidence(
                kind=EvidenceKind.VERIFIED_FACT,
                claim="最新行情快照",
                value=findings["latest_quote"],
                source_id=str(quote.get("source_id") or "unknown"),
                data_timestamp=str(quote.get("source_timestamp") or ""),
            ))
        else:
            missing.append("latest_quote")

        # Model interpretation — advisory, clearly labeled.
        interp = await self._router.infer(
            self.task_kind,
            interpretation_prompt or self._default_prompt(instrument_id),
            fallback_text="",
        )
        if interp.text:
            findings["model_interpretation"] = {
                "text": interp.text, "degraded": interp.degraded,
            }
            evidence.append(AnalysisEvidence(
                kind=EvidenceKind.MODEL_INTERPRETATION,
                claim="模型解讀",
                value={"degraded": interp.degraded},
                source_id=self._router.model_for(self.task_kind),
            ))

        quality = "complete"
        if missing:
            quality = "partial"
        if interp.degraded:
            quality = "degraded" if not closes else quality

        return {
            "ok": True, "degraded": interp.degraded or bool(missing),
            "instrument_id": instrument_id, "market": self.market,
            "findings": findings, "missing_data": missing,
            "data_quality": quality,
            "evidence": [e.to_dict() for e in evidence],
        }

    def _default_prompt(self, instrument_id: str) -> str:
        return f"解讀 {instrument_id} 的技術指標與市場狀態"


class TaiwanEquityIntelligence(_BaseIntelligence):
    """台股：上市/上櫃/ETF + 大盤與產業（指標確定性，AI 只解讀）。"""

    market = "tw"
    task_kind = AnalysisTaskKind.QUICK_MARKET
    INDEX_ID = "tw:INDEX:TAIEX:TWD"

    async def market_overview(self) -> dict[str, Any]:
        return await self.analyze(self.INDEX_ID)


class USEquityIntelligence(_BaseIntelligence):
    """美股：個股/ETF + S&P/NASDAQ + 總經（macro 區塊標記未驗證）。"""

    market = "us"
    task_kind = AnalysisTaskKind.QUICK_MARKET
    SPX_ID = "us:INDEX:SPX:USD"
    NDX_ID = "us:INDEX:NDX:USD"

    def macro_block(self) -> dict[str, Any]:
        """Macro context is declared, never fabricated — requires source."""
        return {
            "fields": ["rate", "dxy", "cpi", "employment", "treasury_yield",
                       "earnings", "guidance", "sector_flows"],
            "data_status": "UNVERIFIED_INFORMATION",
            "note": "總經/財報資料需正式來源；新聞與社群內容不得視為已驗證事實",
        }
