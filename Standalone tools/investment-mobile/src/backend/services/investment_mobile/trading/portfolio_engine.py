"""portfolio domain — positions derived from the execution ledger.

Positions are keyed by (account_id, instrument_id) — per-account
isolation is structural. ``snapshot()`` produces a ``PortfolioSnapshot``
contract for reporting and downstream risk checks.
"""

from __future__ import annotations

from typing import Any

from .contracts import CashBalance, Execution, PortfolioSnapshot, Position


class PortfolioEngine:
    def __init__(self) -> None:
        # (account_id, instrument_id) -> Position
        self._positions: dict[tuple[str, str], Position] = {}

    # ------------------------------------------------------------------
    def apply_execution(self, execution: Execution) -> Position:
        key = (execution.account_id, execution.instrument_id)
        pos = self._positions.get(key)
        if pos is None:
            pos = Position(
                account_id=execution.account_id,
                instrument_id=execution.instrument_id,
                market=execution.market,
            )
            self._positions[key] = pos
        qty = float(execution.quantity)
        price = float(execution.price)
        if execution.side in ("buy", "subscribe"):
            total_cost = pos.average_cost * pos.quantity + price * qty
            pos.quantity += qty
            pos.average_cost = total_cost / pos.quantity if pos.quantity else 0.0
        else:
            pos.quantity = max(0.0, pos.quantity - qty)
        pos.last_price = price
        if pos.quantity <= 0:
            pos.quantity = 0.0
            pos.average_cost = 0.0
        return pos

    # ------------------------------------------------------------------
    def positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        items = self._positions.values()
        if account_id:
            items = [p for p in items if p.account_id == account_id]
        return [p.to_dict() for p in items if p.quantity > 0]

    def position_objects(self, account_id: str | None = None) -> list[Position]:
        items = list(self._positions.values())
        if account_id:
            items = [p for p in items if p.account_id == account_id]
        return [p for p in items if p.quantity > 0]

    def portfolio_value(self, account_id: str | None = None) -> float:
        return sum(p.notional for p in self.position_objects(account_id))

    def snapshot(
        self, account_id: str, cash: list[CashBalance]
    ) -> PortfolioSnapshot:
        positions = self.position_objects(account_id)
        snap = PortfolioSnapshot(
            account_id=account_id,
            positions=positions,
            cash=cash,
            total_market_value=sum(p.market_value for p in positions),
            total_cash=sum(c.available for c in cash),
        )
        return snap

    # ------------------------------------------------------------------
    def load_executions(self, executions: list[Execution]) -> None:
        self._positions.clear()
        for execution in executions:
            self.apply_execution(execution)
