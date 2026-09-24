"""ai-analysis domain — the 星澄 intake boundary.

星澄 may submit ``TradingSignal`` objects and ``TradeProposal``-shaped
analysis payloads. This boundary is the ONLY AI entry point:

- signals go to the StrategyEngine signal book (advisory)
- proposals are recorded for traceability and enter the same governed
  pipeline as strategy-generated proposals — they never skip risk
- no AI path can modify risk limits, create OrderRequest, or reach a
  BrokerAdapter (those types/functions are unreachable from here)
"""

from __future__ import annotations

from typing import Any

from .contracts import OrderSide, TradeProposal
from .strategy_engine import StrategyEngine


class SignalIntake:
    """AI-facing intake — proposals stay proposal-shaped end to end."""

    def __init__(self, strategy: StrategyEngine) -> None:
        self._strategy = strategy

    def submit_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._strategy.record_signal(payload)

    def submit_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record an AI proposal — it still must pass risk + mode gates."""
        proposal = TradeProposal(
            instrument_id=str(payload.get("instrument_id") or ""),
            market=str(payload.get("market") or ""),
            side=str(payload.get("side") or OrderSide.BUY.value),
            quantity=float(payload.get("quantity") or 0.0),
            price=payload.get("price"),
            strategy_id=str(payload.get("strategy_id") or "ai-proposal"),
            signal_id=str(payload.get("signal_id") or ""),
        )
        if not proposal.instrument_id or proposal.quantity <= 0:
            return {"ok": False, "error_code": "INVALID_PROPOSAL"}
        return {"ok": True, "proposal": proposal}
