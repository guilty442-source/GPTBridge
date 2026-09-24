"""backtest domain — strategy research against recorded observations.

Backtests replay ``MarketObservation`` series and signals through the
same TradeProposal → RiskDecision path, producing simulated executions
flagged ``simulated=True`` — never recorded as real trading outcomes.
This phase provides the structural seam; full historical replay arrives
with the research data feed.
"""

from __future__ import annotations

from typing import Any

from .contracts import OrderSide, TradeProposal
from .market_data import MarketDataCache
from .risk_engine import RiskEngine


class BacktestEngine:
    def __init__(self, market_data: MarketDataCache, risk: RiskEngine) -> None:
        self._market = market_data
        self._risk = risk

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replay a signal list against cached prices — simulated only."""
        signals = payload.get("signals")
        if not isinstance(signals, list) or not signals:
            return {"ok": False, "error_code": "SIGNALS_REQUIRED"}
        fills: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for row in signals:
            instrument_id = str(row.get("instrument_id") or "")
            price = self._market.price(instrument_id)
            if price is None:
                price = float(row.get("price") or 0.0)
            if price <= 0:
                rejected.append({"instrument_id": instrument_id, "reason": "no_price"})
                continue
            proposal = TradeProposal(
                instrument_id=instrument_id,
                market=str(row.get("market") or "tw"),
                side=str(row.get("side") or OrderSide.BUY.value),
                quantity=float(row.get("quantity") or 0.0),
                price=price,
                strategy_id=str(row.get("strategy_id") or "backtest"),
            )
            decision = self._risk.evaluate(
                proposal, positions=[], open_orders=0,
                daily_pnl=0.0,
                cash_available=float(payload.get("initial_cash") or 1e9),
                portfolio_value=float(payload.get("initial_cash") or 1e9),
            )
            if not decision.approved:
                rejected.append({
                    "instrument_id": instrument_id,
                    "reasons": decision.reasons,
                })
                continue
            fills.append({
                "instrument_id": instrument_id,
                "side": proposal.side,
                "quantity": proposal.quantity,
                "price": price,
                "simulated": True,
            })
        return {
            "ok": True,
            "simulated": True,
            "fills": fills,
            "rejected": rejected,
        }
