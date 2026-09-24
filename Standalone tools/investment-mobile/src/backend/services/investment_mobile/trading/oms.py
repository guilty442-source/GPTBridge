"""Order Management System — decision-free order state machine.

Mirrors the C# orchestration component (``native/oms``): the same states
and transitions. Every transition is journaled to the trading audit and,
when bound, forwarded to ai-assistant as the authoritative record.

Pipeline: intent → risk evaluation → mode gate → adapter dispatch (LIVE,
verified only) or simulated fill (PAPER/SHADOW).
"""

from __future__ import annotations

import time
from typing import Any

from .audit import TradingAudit
from .broker.base import BrokerRegistry
from .contracts import Fill, Order, OrderIntent, OrderStatus
from .modes import ModeGate
from .portfolio_engine import PortfolioEngine
from .risk_engine import RiskEngine


class OrderManagementSystem:
    def __init__(
        self,
        *,
        mode_gate: ModeGate,
        risk_engine: RiskEngine,
        portfolio: PortfolioEngine,
        brokers: BrokerRegistry,
        audit: TradingAudit,
    ) -> None:
        self._mode_gate = mode_gate
        self._risk = risk_engine
        self._portfolio = portfolio
        self._brokers = brokers
        self._audit = audit
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._daily_order_count = 0
        self._daily_realized_pnl = 0.0
        self._day = time.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    def _rollover(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._daily_order_count = 0
            self._daily_realized_pnl = 0.0

    def submit(self, intent: OrderIntent) -> dict[str, Any]:
        self._rollover()
        order = Order(intent=intent)
        self._orders[order.order_id] = order
        self._audit.record("order.created", order.to_dict())

        # 1. risk gate — always evaluated, in every mode.
        decision = self._risk.evaluate(
            intent,
            self._portfolio.positions(),
            daily_order_count=self._daily_order_count,
            daily_realized_pnl=self._daily_realized_pnl,
        )
        self._audit.record(
            "risk.evaluated",
            {"order_id": order.order_id, **decision.to_dict()},
        )
        if not decision.approved:
            order.status = OrderStatus.RISK_REJECTED.value
            order.rejection = "; ".join(decision.reasons)
            self._audit.record("order.rejected", order.to_dict())
            return {"ok": False, "order": order.to_dict(), "decision": decision.to_dict()}

        # 2. mode gate — ANALYSIS never submits.
        if not self._mode_gate.allows("orders"):
            order.status = OrderStatus.MODE_BLOCKED.value
            order.rejection = f"mode {self._mode_gate.mode.value} blocks order submission"
            self._audit.record("order.mode_blocked", order.to_dict())
            return {"ok": False, "order": order.to_dict(), "decision": decision.to_dict()}

        self._daily_order_count += 1

        # 3a. SHADOW — record the would-be decision, no fill.
        if self._mode_gate.mode.value == "SHADOW":
            order.status = OrderStatus.SUBMITTED.value
            order.rejection = ""
            self._audit.record("order.shadow", order.to_dict())
            return {"ok": True, "order": order.to_dict(), "decision": decision.to_dict()}

        # 3b. PAPER — simulated fill at the intent price.
        if self._mode_gate.mode.value == "PAPER":
            fill = Fill(
                order_id=order.order_id,
                instrument=intent.instrument,
                market=intent.market,
                side=intent.side,
                quantity=intent.quantity,
                price=float(intent.price or 0.0),
                simulated=True,
            )
            order.status = OrderStatus.FILLED.value
            self._fills.append(fill)
            self._portfolio.apply_fill(fill)
            self._audit.record("order.filled", {"order": order.to_dict(), "fill": fill.to_dict()})
            return {
                "ok": True,
                "order": order.to_dict(),
                "fill": fill.to_dict(),
                "decision": decision.to_dict(),
            }

        # 3c. LIVE — dispatch through the verified adapter only.
        adapter = self._brokers.for_market(intent.market)
        if adapter is None:
            order.status = OrderStatus.ADAPTER_DENIED.value
            order.rejection = f"no broker adapter for market '{intent.market}'"
            self._audit.record("order.adapter_denied", order.to_dict())
            return {"ok": False, "order": order.to_dict(), "decision": decision.to_dict()}
        result = adapter.place_order(order, intent)
        if not result.get("ok"):
            order.status = OrderStatus.ADAPTER_DENIED.value
            order.rejection = str(result.get("error_code") or "ADAPTER_DENIED")
            self._audit.record("order.adapter_denied", {**order.to_dict(), "adapter": result})
            return {"ok": False, "order": order.to_dict(), "decision": decision.to_dict(), "adapter": result}
        order.status = OrderStatus.SUBMITTED.value
        order.broker_order_id = str(result.get("broker_order_id") or "")
        self._audit.record("order.submitted", {**order.to_dict(), "adapter": result})
        return {"ok": True, "order": order.to_dict(), "decision": decision.to_dict(), "adapter": result}

    # ------------------------------------------------------------------
    def cancel(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(str(order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if order.status in (
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.RISK_REJECTED.value,
        ):
            return {"ok": False, "error_code": "ORDER_TERMINAL", "order": order.to_dict()}
        order.status = OrderStatus.CANCELLED.value
        order.updated_at = time.time()
        self._audit.record("order.cancelled", order.to_dict())
        return {"ok": True, "order": order.to_dict()}

    def orders(self, limit: int = 100) -> list[dict[str, Any]]:
        orders = sorted(self._orders.values(), key=lambda o: o.created_at, reverse=True)
        return [o.to_dict() for o in orders[: max(1, int(limit))]]

    def fills(self) -> list[Fill]:
        return list(self._fills)
