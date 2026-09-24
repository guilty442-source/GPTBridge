"""InvestmentRecommendationEngine — structured buy/sell advice.

Stock/ETF actions: BUY | SELL | ADD | REDUCE | HOLD | EXIT.
Fund actions:     SUBSCRIBE | ADD | HOLD | REDUCE | REDEEM | SWITCH.

Every recommendation: recommendation_id, instrument_id, account_id,
recommendation_type, analysis_timestamp, data_timestamp, strategy_id,
strategy_version, model_id, model_version, reasoning, risk_factors,
evidence_refs, status.

Deterministic signal math decides the action class; 星澄 writes the
reasoning when available. AI free text NEVER becomes a trade command —
recommendations enter the governed lifecycle (CREATED → VALIDATED →
PUBLISHED …) via the existing intelligence lifecycle/outcome services.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from ..intelligence.contracts import (
    InvestmentRecommendation, RecommendationStatus)

ENGINE_TAG = "monitoring/v1"
EQUITY_ACTIONS = frozenset({"BUY", "SELL", "ADD", "REDUCE",
                            "HOLD", "EXIT"})
FUND_ACTIONS = frozenset({"SUBSCRIBE", "ADD", "HOLD", "REDUCE",
                          "REDEEM", "SWITCH"})


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class InvestmentRecommendationEngine:
    def __init__(self, state_dir: Path, lifecycle: Any,
                 outcome: Any, router: Any | None = None) -> None:
        self._lifecycle = lifecycle
        self._outcome = outcome
        self._router = router      # ModelRouter — may be unavailable

    # ------------------------------------------------------------------
    def _signal(self, indicators: dict[str, Any],
                position: dict[str, Any] | None,
                kind: str) -> tuple[str, list[str], list[str]]:
        """Deterministic action + risk factors + evidence. No LLM math."""
        rsi = indicators.get("rsi14")
        last = indicators.get("last")
        ma50 = indicators.get("ma50")
        ma200 = indicators.get("ma200")
        reasons: list[str] = []
        risks: list[str] = []
        action = "HOLD"
        if kind == "fund":
            action = "HOLD"
        if rsi is not None:
            if rsi <= 30:
                reasons.append(f"RSI {rsi:.0f} oversold")
                action = "ADD" if position else (
                    "SUBSCRIBE" if kind == "fund" else "BUY")
            elif rsi >= 70:
                reasons.append(f"RSI {rsi:.0f} overbought")
                action = ("REDUCE" if position else "HOLD")
                risks.append("short-term overheating")
        if last and ma50 and ma200:
            if last > ma50 > ma200:
                reasons.append("price above MA50>MA200 uptrend")
                if action in ("HOLD",) and not position:
                    action = "BUY" if kind != "fund" else "SUBSCRIBE"
            elif last < ma50 < ma200:
                reasons.append("downtrend below MA50<MA200")
                risks.append("sustained downtrend")
                if position:
                    action = "REDUCE" if action == "HOLD" else action
        if position:
            cost = _d(position.get("average_cost"))
            qty = _d(position.get("quantity"))
            if cost > 0 and last:
                pnl = (Decimal(str(last)) - cost) / cost
                if pnl <= Decimal("-0.2"):
                    risks.append(f"holding loss {pnl:.1%}")
                    action = "EXIT" if kind != "fund" else "REDEEM"
                elif pnl >= Decimal("0.5"):
                    risks.append(f"large gain {pnl:.1%} — consider trim")
        if not reasons:
            reasons.append("no decisive deterministic signal")
        return action, reasons, risks

    # ------------------------------------------------------------------
    def recommend(self, *, instrument_id: str, account_id: str = "",
                  kind: str = "equity",
                  indicators: dict[str, Any] | None = None,
                  position: dict[str, Any] | None = None,
                  strategy_id: str = "", strategy_version: str = "",
                  data_timestamp: float | None = None,
                  nav: dict[str, Any] | None = None,
                  reasoning_override: str | None = None
                  ) -> dict[str, Any]:
        action, reasons, risks = self._signal(
            indicators or {}, position, kind)
        allowed = FUND_ACTIONS if kind == "fund" else EQUITY_ACTIONS
        if action not in allowed:
            action = "HOLD"
        model_id, model_ver = "deterministic-engine", ENGINE_TAG
        if reasoning_override:
            reasoning = reasoning_override
        else:
            reasoning = "; ".join(reasons)
        if self._router is not None and self._router.available:
            model_id = self._router.model_for("quick_market")
            model_ver = "router"
        else:
            risks.append("model unavailable — deterministic-only")
        from datetime import datetime, timezone
        rec = InvestmentRecommendation(
            account_id=account_id,
            instrument_id=instrument_id,
            instrument_type="fund" if kind == "fund" else "stock",
            market=str((position or {}).get("market") or ""),
            recommendation_type=action,
            reasoning=reasoning,
            risk_factors=risks,
            evidence_refs=[f"indicators:{instrument_id}"] +
                ([f"nav:{nav['nav_date']}"] if nav else []),
            reference_nav=_d(nav["nav"]) if nav else None,
            model_id=model_id,
            model_version=model_ver,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            market_data_timestamp=datetime.fromtimestamp(
                data_timestamp or time.time(), timezone.utc),
        )
        lr = self._lifecycle.create(rec)
        if not lr.get("ok"):
            return lr
        out = dict(lr["recommendation"])
        out["kind"] = kind
        out["advisory"] = True
        out["note"] = "recommendation is advisory — never a trade command"
        return {"ok": True, "recommendation": out,
                "lifecycle": lr}

    # ------------------------------------------------------------------
    def transition(self, rec_id: str, target: str, *,
                   actor: str = "user", reason: str = ""
                   ) -> dict[str, Any]:
        if actor.lower() in ("ai", "xingcheng", "model", "assistant") \
                and target not in (RecommendationStatus.EXPIRED,
                                   RecommendationStatus.INVALIDATED):
            return {"ok": False, "error_code": "AI_LIFECYCLE_DENIED"}
        return self._lifecycle.transition(
            rec_id, target, reason=reason)

    def expire_due(self) -> dict[str, Any]:
        return self._lifecycle.expire_due()

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._lifecycle.list(status)

    def evaluate_outcome(self, rec_id: str,
                         horizon_days: int = 30) -> dict[str, Any]:
        """MFE/MAE follow-up — advisory outcome only, never booked as
        real investment return."""
        r = self._outcome.evaluate(rec_id, horizon_days=horizon_days)
        if r.get("ok"):
            r["outcome_kind"] = "ai_analysis"
            r["note"] = "unexecuted advice — not a real return"
        return r
