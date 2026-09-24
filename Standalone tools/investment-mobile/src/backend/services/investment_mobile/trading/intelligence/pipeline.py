"""Unified analysis pipeline — typed stages, fail-closed on bad data.

MarketData → DataValidation → PortfolioContext → MarketAnalysis →
InstrumentAnalysis → StrategyEvaluation → RiskAnalysis →
TradeProposal → Recommendation

Each stage emits a named artifact; a stage may mark the run degraded but
never invents missing inputs. Free-form text never carries trading
parameters — proposals are validated structured objects only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .contracts import (
    AnalysisEvidence, AnalysisRun, AnalysisTaskKind, EvidenceKind,
    InvestmentRecommendation, RecommendationType,
)
from .evidence import AnalysisEvidenceService
from .safety import AISafetyBoundary

STAGES = (
    "market_data", "data_validation", "portfolio_context",
    "market_analysis", "instrument_analysis", "strategy_evaluation",
    "risk_analysis", "trade_proposal", "recommendation",
)


class AnalysisPipeline:
    """Drives one AnalysisRun through the stage sequence."""

    def __init__(self, evidence: AnalysisEvidenceService,
                 safety: AISafetyBoundary) -> None:
        self._evidence = evidence
        self._safety = safety

    def begin(self, task_kind: str, *, instrument_id: str = "",
              market: str = "", account_id: str = "",
              model_id: str = "", strategy_id: str = "",
              strategy_version: str = "") -> AnalysisRun:
        run = AnalysisRun(
            task_kind=task_kind, instrument_id=instrument_id,
            market=market, account_id=account_id, model_id=model_id,
            strategy_id=strategy_id, strategy_version=strategy_version)
        return run

    def stage(self, run: AnalysisRun, name: str, *,
              findings: dict[str, Any] | None = None,
              evidence: list[AnalysisEvidence] | None = None,
              missing: list[str] | None = None,
              skipped: bool = False) -> AnalysisRun:
        if skipped:
            run.stages_skipped.append(name)
        else:
            run.stages_completed.append(name)
        if findings:
            run.findings[name] = findings
        for ev in evidence or []:
            res = self._evidence.attest(ev)
            if res.get("ok"):
                run.evidence.append(ev)
        run.missing_data.extend(missing or [])
        return run

    def finish(self, run: AnalysisRun) -> AnalysisRun:
        run.finished_at = datetime.now(timezone.utc)
        run.degraded = bool(run.missing_data)
        self._journal(run)
        return run

    def runs(self, limit: int = 200) -> list[dict[str, Any]]:
        path = self._runs_path()
        if not path.exists():
            return []
        import json as _json
        lines = path.read_text("utf-8").splitlines()[-int(limit):]
        return [_json.loads(l) for l in lines if l.strip()]

    def _journal(self, run: AnalysisRun) -> None:
        import json as _json
        path = self._runs_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(run.to_dict(), ensure_ascii=False) + "\n")

    def _runs_path(self):
        return self._evidence._path.parent / "analysis_runs.jsonl"

    # ------------------------------------------------------------------
    def build_recommendation(
        self, run: AnalysisRun, *,
        account_id: str, instrument_id: str, instrument_type: str,
        market: str, recommendation_type: str,
        reasoning: str, risk_factors: list[str],
        reference_price: Any = None, reference_nav: Any = None,
        suggested_weight: Any = None,
        observation_window: str = "",
        trigger_conditions: list[str] | None = None,
        reevaluate_conditions: list[str] | None = None,
        market_data_timestamp: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Recommendation requires ≥1 attested evidence ref."""
        rec = InvestmentRecommendation(
            account_id=account_id, instrument_id=instrument_id,
            instrument_type=instrument_type, market=market,
            recommendation_type=recommendation_type,
            reasoning=reasoning, risk_factors=list(risk_factors),
            evidence_refs=[e.evidence_id for e in run.evidence],
            reference_price=Decimal(str(reference_price))
            if reference_price is not None else None,
            reference_nav=Decimal(str(reference_nav))
            if reference_nav is not None else None,
            suggested_weight=Decimal(str(suggested_weight))
            if suggested_weight is not None else None,
            observation_window=observation_window,
            trigger_conditions=list(trigger_conditions or []),
            reevaluate_conditions=list(reevaluate_conditions or []),
            market_data_timestamp=market_data_timestamp,
            model_id=run.model_id, model_version=run.model_version,
            strategy_id=run.strategy_id,
            strategy_version=run.strategy_version,
            data_quality=self._evidence.grade_data_quality(run.evidence),
            expires_at=expires_at,
        )
        errors = rec.validate()
        if errors:
            return {"ok": False, "errors": errors}
        return {"ok": True, "recommendation": rec}
