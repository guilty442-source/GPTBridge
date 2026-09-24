"""Portfolio Engine — position aggregation and asset summary.

Positions are reconstructed from the tool-local fill journal; the
authoritative holdings/transaction records live in ai-assistant. This
engine exists so the risk engine can evaluate against live exposure
without a cross-tool call on the hot path.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .contracts import Fill, Position


class PortfolioEngine:
    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], Position] = {}

    # ------------------------------------------------------------------
    def apply_fill(self, fill: Fill) -> Position:
        key = (str(fill.market), str(fill.instrument))
        position = self._positions.get(key)
        if position is None:
            position = Position(instrument=fill.instrument, market=fill.market)
            self._positions[key] = position
        qty = float(fill.quantity)
        if fill.side == "sell":
            qty = -qty
        new_qty = position.quantity + qty
        if qty > 0:
            cost = position.quantity * position.average_cost + qty * fill.price
            position.average_cost = cost / new_qty if new_qty else 0.0
        position.quantity = new_qty
        position.last_price = fill.price
        if position.quantity <= 0:
            position.quantity = 0.0
            position.average_cost = 0.0
        return position

    def rebuild(self, fills: Iterable[Fill]) -> None:
        self._positions.clear()
        for fill in fills:
            self.apply_fill(fill)

    # ------------------------------------------------------------------
    def mark(self, market: str, instrument: str, price: float) -> None:
        position = self._positions.get((str(market), str(instrument)))
        if position is not None:
            position.last_price = float(price)

    def positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.quantity > 0]

    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        positions = self.positions()
        by_market: dict[str, float] = defaultdict(float)
        for position in positions:
            by_market[position.market] += position.market_value
        return {
            "positions": [p.to_dict() for p in positions],
            "by_market": dict(by_market),
            "total_market_value": sum(by_market.values()),
        }
