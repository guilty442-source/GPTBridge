"""trading domain — Order Management System.

Fixed pipeline (never bypassed):

    TradeProposal → RiskEngine → OrderRequest → (mode gate)
      → BrokerAdapter (LIVE) / paper ledger (PAPER)
      → OrderReceipt → Execution → Portfolio

- ANALYSIS: proposals stop after risk evaluation (decision recorded).
- SHADOW:   risk-decided OrderRequest recorded — never submitted.
- PAPER:    filled through the dedicated simulated account only.
- LIVE:     dispatch to a verified BrokerAdapter — AI cannot reach this
            path directly; only the OMS calls ``adapter.place_order``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .accounts import AccountRegistry
from .audit import TradingAudit
from .broker.base import BrokerRegistry
from .contracts import (
    Execution,
    OrderReceipt,
    OrderRequest,
    OrderStatus,
    TradeProposal,
    TradingMode,
)
from .modes import ModeGate
from .portfolio_engine import PortfolioEngine
from .risk_engine import RiskEngine


class OrderManagementSystem:
    def __init__(
        self,
        state_dir: Path,
        mode_gate: ModeGate,
        risk: RiskEngine,
        portfolio: PortfolioEngine,
        accounts: AccountRegistry,
        brokers: BrokerRegistry,
        audit: TradingAudit,
        market: Any | None = None,
    ) -> None:
        self._dir = state_dir
        self._gate = mode_gate
        self._risk = risk
        self._portfolio = portfolio
        self._accounts = accounts
        self._brokers = brokers
        self._audit = audit
        self._market = market
        self._orders_path = state_dir / "orders.jsonl"
        self._executions_path = state_dir / "executions.jsonl"
        self._decisions_path = state_dir / "decisions.jsonl"
        self._open_orders: list[OrderRequest] = []
        self._executions: list[Execution] = []
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        for line in self._read(self._executions_path):
            try:
                self._executions.append(Execution(**line))
            except TypeError:
                continue
        for line in self._read(self._orders_path):
            try:
                proposal = TradeProposal(**line["proposal"])
                order = OrderRequest(
                    proposal=proposal,
                    status=line.get("status", OrderStatus.CREATED.value),
                    order_id=line["order_id"],
                    decision_id=line.get("decision_id", ""),
                )
            except (TypeError, KeyError):
                continue
            if order.status in (OrderStatus.CREATED.value, OrderStatus.SUBMITTED.value):
                self._open_orders.append(order)
        self._portfolio.load_executions(self._executions)

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for raw in lines:
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return rows

    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _persist_order(self, order: OrderRequest) -> None:
        self._append(self._orders_path, order.to_dict())

    # ------------------------------------------------------------------
    def submit(self, proposal: TradeProposal) -> dict[str, Any]:
        """Full governed pipeline for one proposal."""
        account = self._accounts.for_market(proposal.market, paper=False)
        if account is None:
            return {"ok": False, "error_code": "NO_ACCOUNT_FOR_MARKET"}
        proposal.account_id = account.account_id

        # -- risk evaluation (always, even in ANALYSIS — produces evidence)
        cash = self._accounts.cash(account.account_id, account.currency)
        # Total assets = positions + cash — exposure % is measured against
        # the whole account, not positions alone (empty book ≠ 1.0 base).
        portfolio_value = (
            self._portfolio.portfolio_value(account.account_id) + cash.available
        )
        decision = self._risk.evaluate(
            proposal,
            positions=self._portfolio.position_objects(account.account_id),
            open_orders=len(self._open_orders),
            daily_pnl=self._daily_pnl(account.account_id),
            cash_available=cash.available,
            portfolio_value=portfolio_value,
        )
        self._append(self._decisions_path, {
            "proposal_id": proposal.proposal_id,
            "decision": decision.to_dict(),
        })
        self._audit.record("risk.evaluated", {
            "proposal_id": proposal.proposal_id,
            "approved": decision.approved,
            "reasons": decision.reasons,
            "backend": decision.backend,
        })
        if not decision.approved:
            return {
                "ok": False,
                "error_code": "RISK_REJECTED",
                "reasons": decision.reasons,
                "decision": decision.to_dict(),
            }

        # -- ANALYSIS: evidence only, no order record
        if not self._gate.allows("decisions"):
            self._audit.record("order.mode_blocked", {
                "proposal_id": proposal.proposal_id,
                "mode": self._gate.mode.value,
            })
            return {
                "ok": False,
                "error_code": "MODE_BLOCKED",
                "mode": self._gate.mode.value,
                "decision": decision.to_dict(),
            }

        order = OrderRequest(proposal=proposal, decision_id=decision.decision_id)
        self._persist_order(order)
        self._audit.record("order.created", order.to_dict())

        # -- SHADOW: order decision recorded, never submitted
        if not self._gate.allows("orders"):
            order.status = OrderStatus.MODE_BLOCKED.value
            self._persist_order(order)
            return {
                "ok": True,
                "shadowed": True,
                "order_id": order.order_id,
                "mode": self._gate.mode.value,
            }

        # -- PAPER: simulated account fill, never touches a broker
        if self._gate.mode is TradingMode.PAPER:
            paper = self._accounts.paper_account(proposal.market)
            proposal.account_id = paper.account_id
            execution = Execution(
                order_id=order.order_id,
                instrument_id=proposal.instrument_id,
                market=proposal.market,
                side=proposal.side,
                quantity=proposal.quantity,
                price=float(proposal.price or 0.0),
                account_id=paper.account_id,
                simulated=True,
            )
            return self._fill(order, execution)

        # -- LIVE: verified adapter dispatch only + fresh market data
        adapter = self._brokers.adapter_for(account.broker_id)
        if adapter is None or not adapter.api_verified:
            order.status = OrderStatus.ADAPTER_DENIED.value
            self._persist_order(order)
            self._audit.record("order.adapter_denied", {
                "order_id": order.order_id,
                "broker_id": account.broker_id,
            })
            return {
                "ok": False,
                "error_code": "BROKER_API_UNVERIFIED",
                "order_id": order.order_id,
            }
        if self._market is not None:
            fresh = self._market.fresh_price(
                proposal.instrument_id,
                max_age_s=float(self._risk.limit("max_quote_age_s", 30.0)),
            )
            if not fresh.get("ok"):
                order.status = OrderStatus.ADAPTER_DENIED.value
                self._persist_order(order)
                self._audit.record("order.stale_market_data", {
                    "order_id": order.order_id,
                    "error": fresh.get("error_code"),
                })
                return {
                    "ok": False,
                    "error_code": fresh.get("error_code", "STALE_MARKET_DATA"),
                    "order_id": order.order_id,
                }
        order.status = OrderStatus.SUBMITTED.value
        self._open_orders.append(order)
        self._persist_order(order)
        receipt: OrderReceipt = adapter.place_order(order)
        self._audit.record("order.receipt", receipt.to_dict())
        if receipt.rejection:
            order.status = OrderStatus.ADAPTER_DENIED.value
            self._persist_order(order)
            return {
                "ok": False,
                "error_code": "BROKER_REJECTED",
                "receipt": receipt.to_dict(),
            }
        return {"ok": True, "order_id": order.order_id, "receipt": receipt.to_dict()}

    # ------------------------------------------------------------------
    def record_execution(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Broker-reported execution (LIVE) — matched to a submitted order."""
        order_id = str(payload.get("order_id") or "")
        order = next((o for o in self._open_orders if o.order_id == order_id), None)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_OPEN"}
        execution = Execution(
            order_id=order_id,
            instrument_id=order.proposal.instrument_id,
            market=order.proposal.market,
            side=order.proposal.side,
            quantity=float(payload.get("quantity") or 0.0),
            price=float(payload.get("price") or 0.0),
            account_id=order.proposal.account_id,
            commission=float(payload.get("commission") or 0.0),
            fees=dict(payload.get("fees") or {}),
            simulated=False,
        )
        result = self._fill(order, execution)
        if result.get("ok"):
            self._open_orders.remove(order)
        return result

    def _fill(self, order: OrderRequest, execution: Execution) -> dict[str, Any]:
        position = self._portfolio.apply_execution(execution)
        account = self._accounts.get(execution.account_id)
        if account is not None:
            delta = execution.quantity * execution.price
            if execution.side in ("buy", "subscribe"):
                delta = -delta
            self._accounts.apply_execution_cash(
                account.account_id, account.currency, delta
            )
        order.status = OrderStatus.FILLED.value
        self._persist_order(order)
        self._append(self._executions_path, execution.to_dict())
        self._executions.append(execution)
        self._audit.record("order.filled", {
            "order_id": order.order_id,
            "execution": execution.to_dict(),
        })
        return {
            "ok": True,
            "order_id": order.order_id,
            "execution": execution.to_dict(),
            "position": position.to_dict(),
        }

    # ------------------------------------------------------------------
    def _daily_pnl(self, account_id: str) -> float:
        # Conservative placeholder: realized P&L is not tracked yet —
        # report 0 so the daily-loss limit never silently blocks analysis.
        return 0.0

    def open_orders(self) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self._open_orders]

    def executions(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._executions]
