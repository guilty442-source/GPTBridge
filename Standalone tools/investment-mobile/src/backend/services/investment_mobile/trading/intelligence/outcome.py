"""RecommendationOutcomeService — post-hoc tracking, strictly separated.

AI-analysis outcomes, simulated-trade results and real-trade results
live in three disjoint ledgers — they are never blended into a single
"performance" number. Outcomes feed strategy *research* only; they can
never mutate live strategy parameters (versioned, authorized upgrades
are a separate governed path).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..market.engine import MarketDataEngine
from .contracts import (
    OutcomeKind, RecommendationOutcome,
)
from .lifecycle import RecommendationLifecycle


class RecommendationOutcomeService:
    def __init__(self, state_dir: Path, market: MarketDataEngine,
                 lifecycle: RecommendationLifecycle) -> None:
        self._path = Path(state_dir) / "recommendation_outcomes.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._market = market
        self._lifecycle = lifecycle

    # ------------------------------------------------------------------
    def evaluate(self, rec_id: str, *, horizon_days: int = 30,
                 kind: str = OutcomeKind.AI_ANALYSIS) -> dict[str, Any]:
        if kind not in OutcomeKind.ALL:
            return {"ok": False, "error_code": "OUTCOME_KIND_UNKNOWN"}
        rec = self._lifecycle.get(rec_id)
        if rec is None:
            return {"ok": False, "error_code": "REC_NOT_FOUND"}
        iid = rec.get("instrument_id") or ""
        if rec.get("instrument_type") == "fund":
            return {"ok": False, "error_code": "FUND_USE_NAV_EVALUATION"}
        candles = self._market.candles(iid, timeframe="1d",
                                       limit=int(horizon_days) + 60)
        if not candles:
            return {"ok": False, "error_code": "NO_MARKET_DATA"}

        rec_ts = str(rec.get("created_at") or "")[:10]
        after = [c for c in candles if str(c.get("candle_start"))[:10] >= rec_ts]
        if not after:
            return {"ok": False, "error_code": "NO_POST_REC_DATA"}
        p0 = Decimal(str(rec.get("reference_price")
                         or after[0].get("close") or 0))
        if p0 <= 0:
            return {"ok": False, "error_code": "NO_REFERENCE_PRICE"}
        closes = [Decimal(str(c.get("close") or 0)) for c in after]
        last = closes[-1]
        favorable = max(closes) / p0 - 1
        adverse = min(closes) / p0 - 1
        ret = last / p0 - 1
        direction = rec.get("recommendation_type") or ""
        bullish = direction in ("BUY", "ADD", "SUBSCRIBE")
        bearish = direction in ("SELL", "REDUCE", "EXIT", "REDEEM")
        signal_valid = (ret > 0) if bullish else ((ret < 0) if bearish else None)

        outcome = RecommendationOutcome(
            recommendation_id=rec_id, outcome_kind=kind,
            horizon_days=int(horizon_days),
            price_at_recommendation=p0, price_at_evaluation=last,
            return_pct=ret,
            max_favorable_pct=favorable, max_adverse_pct=adverse,
            signal_valid=signal_valid)
        self._append(outcome.to_dict())
        return {"ok": True, "outcome": outcome.to_dict()}

    def outcomes(self, rec_id: str | None = None,
                 kind: str | None = None) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if rec_id and row.get("recommendation_id") != rec_id:
                continue
            if kind and row.get("outcome_kind") != kind:
                continue
            out.append(row)
        return out

    def _append(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
