"""Trading engine service — governed command surface for the engine cluster.

Commands are own-tool / governance-actor commands on the
``investment-mobile-*`` surface. Business requests keep relaying through
``xingcheng → ai-assistant``; the engines never touch models, networks, or
the business database directly.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .audit import TradingAudit
from .broker import (
    BrokerRegistry,
    CathaySecuritiesAdapter,
    FubonSubBrokerageAdapter,
    FundPlatformAdapter,
)
from .contracts import OrderIntent, Signal
from .modes import ModeGate
from .oms import OrderManagementSystem
from .portfolio_engine import PortfolioEngine
from .risk_engine import RiskEngine
from .strategy_engine import StrategyEngine


class TradingEngineService:
    """Composes and owns the engine cluster lifecycle."""

    COMMANDS = frozenset(
        {
            "investment-mobile-engine-status",
            "investment-mobile-trading-mode-get",
            "investment-mobile-trading-mode-set",
            "investment-mobile-signal-ingest",
            "investment-mobile-signal-list",
            "investment-mobile-order-submit",
            "investment-mobile-order-cancel",
            "investment-mobile-order-list",
            "investment-mobile-positions",
            "investment-mobile-portfolio-summary",
            "investment-mobile-risk-evaluate",
            "investment-mobile-risk-summary",
            "investment-mobile-brokers",
            "investment-mobile-strategies",
            "investment-mobile-backtest",
            "investment-mobile-audit-tail",
        }
    )

    def __init__(self, tool_root: Path) -> None:
        state_dir = Path(tool_root) / "runtime" / "state"
        self._state_dir = state_dir
        self.mode_gate = ModeGate(state_dir)
        self.audit = TradingAudit(state_dir)
        self.risk = RiskEngine(state_dir)
        self.strategy = StrategyEngine(state_dir)
        self.portfolio = PortfolioEngine()
        self.brokers = BrokerRegistry(state_dir)
        self.brokers.register(CathaySecuritiesAdapter(state_dir), market="tw")
        self.brokers.register(FubonSubBrokerageAdapter(state_dir), market="us")
        self.brokers.register(FundPlatformAdapter(state_dir), market="fund")
        self.oms = OrderManagementSystem(
            mode_gate=self.mode_gate,
            risk_engine=self.risk,
            portfolio=self.portfolio,
            brokers=self.brokers,
            audit=self.audit,
        )

    # ------------------------------------------------------------------
    def owns(self, command: str) -> bool:
        return str(command) in self.COMMANDS

    def bind_channel(self, channel: Any | None) -> None:
        self.audit.bind_channel(channel)

    # ------------------------------------------------------------------
    async def handle(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if not self.owns(command):
            raise PermissionError("PERMISSION_DENIED")
        payload = dict(payload or {})

        if command == "investment-mobile-engine-status":
            return "engine-status", {
                "ok": True,
                "mode": self.mode_gate.mode.value,
                "live_authorized": self.mode_gate.live_authorization() is not None,
                "strategies": self.strategy.strategies(),
                "brokers": self.brokers.status(),
                "open_orders": len(self.oms.orders()),
                "at": time.time(),
            }

        if command == "investment-mobile-trading-mode-get":
            return "trading-mode", {
                "ok": True,
                "mode": self.mode_gate.mode.value,
                "modes": ["ANALYSIS", "SHADOW", "PAPER", "LIVE"],
                "live_authorized": self.mode_gate.live_authorization() is not None,
            }

        if command == "investment-mobile-trading-mode-set":
            result = self.mode_gate.set_mode(str(payload.get("mode") or ""))
            if result.get("ok"):
                self.audit.record(
                    "mode.changed",
                    {"mode": result["mode"], "actor": payload.get("actor")},
                )
            else:
                self.audit.record("mode.denied", dict(result))
            return "trading-mode", result

        if command == "investment-mobile-signal-ingest":
            signal = Signal(
                instrument=str(payload.get("instrument") or ""),
                market=str(payload.get("market") or ""),
                side=str(payload.get("side") or "buy"),
                confidence=float(payload.get("confidence") or 0.0),
                price=payload.get("price"),
                quantity=float(payload.get("quantity") or 0.0),
                rationale=str(payload.get("rationale") or ""),
                source=str(payload.get("source") or "xingcheng"),
            )
            result = self.strategy.ingest_signal(signal)
            self.audit.record("signal.ingested", result["signal"])
            return "signal", {"ok": True, **result}

        if command == "investment-mobile-signal-list":
            return "signal-list", {
                "ok": True,
                "signals": self.strategy.signal_book(payload.get("limit", 100)),
            }

        if command == "investment-mobile-order-submit":
            intent = OrderIntent(
                instrument=str(payload.get("instrument") or ""),
                market=str(payload.get("market") or ""),
                side=str(payload.get("side") or "buy"),
                quantity=float(payload.get("quantity") or 0.0),
                price=payload.get("price"),
                strategy_id=str(payload.get("strategy_id") or "manual"),
                signal_id=str(payload.get("signal_id") or ""),
            )
            return "order", self.oms.submit(intent)

        if command == "investment-mobile-order-cancel":
            return "order", self.oms.cancel(str(payload.get("order_id") or ""))

        if command == "investment-mobile-order-list":
            return "order-list", {
                "ok": True,
                "orders": self.oms.orders(payload.get("limit", 100)),
            }

        if command == "investment-mobile-positions":
            return "positions", {
                "ok": True,
                "positions": [p.to_dict() for p in self.portfolio.positions()],
            }

        if command == "investment-mobile-portfolio-summary":
            return "portfolio-summary", {"ok": True, **self.portfolio.summary()}

        if command == "investment-mobile-risk-evaluate":
            intent = OrderIntent(
                instrument=str(payload.get("instrument") or ""),
                market=str(payload.get("market") or ""),
                side=str(payload.get("side") or "buy"),
                quantity=float(payload.get("quantity") or 0.0),
                price=payload.get("price"),
                strategy_id=str(payload.get("strategy_id") or "evaluation"),
            )
            decision = self.risk.evaluate(intent, self.portfolio.positions())
            self.audit.record(
                "risk.evaluated",
                {"intent": intent.to_dict(), **decision.to_dict()},
            )
            return "risk", {"ok": True, "decision": decision.to_dict()}

        if command == "investment-mobile-risk-summary":
            return "risk", {
                "ok": True,
                **self.risk.portfolio_risk_summary(self.portfolio.positions()),
            }

        if command == "investment-mobile-brokers":
            return "brokers", {"ok": True, "brokers": self.brokers.status()}

        if command == "investment-mobile-strategies":
            return "strategies", {"ok": True, "strategies": self.strategy.strategies()}

        if command == "investment-mobile-backtest":
            return "backtest", self.strategy.backtest(payload)

        if command == "investment-mobile-audit-tail":
            return "audit", {
                "ok": True,
                "events": self.audit.tail(payload.get("limit", 50)),
            }

        raise PermissionError("PERMISSION_DENIED")
