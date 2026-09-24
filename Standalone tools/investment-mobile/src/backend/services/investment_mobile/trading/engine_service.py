"""Trading engine service — governed command surface for the 11 domains.

Commands are own-tool / governance-actor commands on the
``investment-mobile-*`` surface. Business requests keep relaying through
``xingcheng → ai-assistant``; the engines never touch models, networks,
or the business database directly.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .accounts import AccountRegistry
from .ai_boundary import SignalIntake
from .audit import TradingAudit
from .backtest import BacktestEngine
from .broker.base import BrokerRegistry
from .contracts import OrderSide, TradeProposal
from .domains import DOMAINS
from .fund import FundLedger
from .instruments import InstrumentRegistry
from .market_data import MarketDataCache
from .modes import ModeGate
from .oms import OrderManagementSystem
from .portfolio_engine import PortfolioEngine
from .risk_engine import RiskEngine
from .strategy_engine import StrategyEngine


class TradingEngineService:
    """Composes and owns the engine cluster lifecycle."""

    COMMANDS = frozenset(
        {
            # engine / mode
            "investment-mobile-engine-status",
            "investment-mobile-trading-mode-get",
            "investment-mobile-trading-mode-set",
            # market-data + instrument domains
            "investment-mobile-market-observe",
            "investment-mobile-market-latest",
            "investment-mobile-instrument-register",
            "investment-mobile-instrument-list",
            # broker + account domains
            "investment-mobile-accounts",
            "investment-mobile-cash-list",
            "investment-mobile-cash-set",
            "investment-mobile-brokers",
            # ai-analysis + strategy domains
            "investment-mobile-signal-ingest",
            "investment-mobile-signal-list",
            "investment-mobile-proposal-submit",
            "investment-mobile-strategy-evaluate",
            # risk + trading domains
            "investment-mobile-risk-evaluate",
            "investment-mobile-order-submit",
            "investment-mobile-order-list",
            "investment-mobile-execution-record",
            # portfolio domain
            "investment-mobile-positions",
            "investment-mobile-portfolio-snapshot",
            # mutual-fund domain
            "investment-mobile-fund-import-nav",
            "investment-mobile-fund-import-txn",
            "investment-mobile-fund-units",
            # backtest + audit domains
            "investment-mobile-backtest",
            "investment-mobile-audit-tail",
            "investment-mobile-domains",
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
        self.accounts = AccountRegistry(state_dir)
        self.instruments = InstrumentRegistry(state_dir)
        self.market_data = MarketDataCache(state_dir)
        self.fund = FundLedger(state_dir)
        self.brokers = BrokerRegistry(state_dir)
        self.ai_intake = SignalIntake(self.strategy)
        self.backtest_engine = BacktestEngine(self.market_data, self.risk)
        self.oms = OrderManagementSystem(
            state_dir,
            mode_gate=self.mode_gate,
            risk=self.risk,
            portfolio=self.portfolio,
            accounts=self.accounts,
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

        # ---------------- engine / mode ----------------
        if command == "investment-mobile-engine-status":
            return "engine-status", {
                "ok": True,
                "mode": self.mode_gate.mode.value,
                "live_readiness": self.mode_gate.live_readiness(),
                "risk_backend": self.risk.backend,
                "strategy_backend": self.strategy.backend,
                "domains": sorted(DOMAINS),
                "brokers": self.brokers.list_brokers(),
                "open_orders": len(self.oms.open_orders()),
                "at": time.time(),
            }

        if command == "investment-mobile-domains":
            return "domains", {"ok": True, "domains": DOMAINS}

        if command == "investment-mobile-trading-mode-get":
            return "trading-mode", {
                "ok": True,
                "mode": self.mode_gate.mode.value,
                "modes": ["ANALYSIS", "SHADOW", "PAPER", "LIVE"],
                "live_readiness": self.mode_gate.live_readiness(),
            }

        if command == "investment-mobile-trading-mode-set":
            result = self.mode_gate.set_mode(str(payload.get("mode") or ""))
            self.audit.record(
                "mode.changed" if result.get("ok") else "mode.denied",
                {**result, "actor": payload.get("actor")},
            )
            return "trading-mode", result

        # ---------------- market-data + instrument ----------------
        if command == "investment-mobile-market-observe":
            return "market", self.market_data.observe(payload)

        if command == "investment-mobile-market-latest":
            return "market", {
                "ok": True,
                "observation": self.market_data.latest(
                    str(payload.get("instrument_id") or "")
                ),
            }

        if command == "investment-mobile-instrument-register":
            return "instrument", self.instruments.register(payload)

        if command == "investment-mobile-instrument-list":
            return "instrument-list", {
                "ok": True,
                "instruments": self.instruments.list(payload.get("market")),
            }

        # ---------------- broker + account ----------------
        if command == "investment-mobile-accounts":
            return "accounts", {
                "ok": True,
                "accounts": self.accounts.list_accounts(),
            }

        if command == "investment-mobile-cash-list":
            return "cash", {"ok": True, "cash": self.accounts.list_cash()}

        if command == "investment-mobile-cash-set":
            bal = self.accounts.set_cash(
                str(payload.get("account_id") or ""),
                str(payload.get("currency") or ""),
                float(payload.get("available") or 0.0),
            )
            self.audit.record("cash.set", bal.to_dict())
            return "cash", {"ok": True, "cash": bal.to_dict()}

        if command == "investment-mobile-brokers":
            return "brokers", {
                "ok": True,
                "brokers": self.brokers.list_brokers(),
            }

        # ---------------- ai-analysis + strategy ----------------
        if command == "investment-mobile-signal-ingest":
            result = self.strategy.record_signal(payload)
            if result.get("ok"):
                self.audit.record("signal.ingested", {
                    "signal_id": result["signal_id"],
                    "source": payload.get("source"),
                })
            return "signal", result

        if command == "investment-mobile-signal-list":
            return "signal-list", {
                "ok": True,
                "signals": self.strategy.signals(payload.get("limit", 100)),
            }

        if command == "investment-mobile-proposal-submit":
            result = self.ai_intake.submit_proposal(payload)
            if not result.get("ok"):
                return "proposal", result
            self.audit.record("proposal.submitted", result["proposal"].to_dict())
            return "proposal", {
                "ok": True,
                "proposal_id": result["proposal"].proposal_id,
                "result": self.oms.submit(result["proposal"]),
            }

        if command == "investment-mobile-strategy-evaluate":
            return "proposals", {
                "ok": True,
                "proposals": self.strategy.evaluate(payload.get("signal_id")),
            }

        # ---------------- risk + trading ----------------
        if command == "investment-mobile-risk-evaluate":
            proposal = self._proposal(payload)
            if proposal is None:
                return "risk", {"ok": False, "error_code": "INVALID_PROPOSAL"}
            decision = self.risk.evaluate(
                proposal,
                positions=self.portfolio.position_objects(),
                open_orders=len(self.oms.open_orders()),
                daily_pnl=0.0,
                cash_available=1e9,
                portfolio_value=self.portfolio.portfolio_value() or 1.0,
            )
            self.audit.record("risk.evaluated", {
                "proposal_id": proposal.proposal_id,
                "approved": decision.approved,
            })
            return "risk", {"ok": True, "decision": decision.to_dict()}

        if command == "investment-mobile-order-submit":
            proposal = self._proposal(payload)
            if proposal is None:
                return "order", {"ok": False, "error_code": "INVALID_PROPOSAL"}
            return "order", self.oms.submit(proposal)

        if command == "investment-mobile-order-list":
            return "order-list", {
                "ok": True,
                "orders": self.oms.open_orders(),
                "executions": self.oms.executions(),
            }

        if command == "investment-mobile-execution-record":
            return "execution", self.oms.record_execution(payload)

        # ---------------- portfolio ----------------
        if command == "investment-mobile-positions":
            return "positions", {
                "ok": True,
                "positions": self.portfolio.positions(payload.get("account_id")),
            }

        if command == "investment-mobile-portfolio-snapshot":
            account_id = str(payload.get("account_id") or "")
            account = self.accounts.get(account_id)
            if account is None:
                return "snapshot", {"ok": False, "error_code": "ACCOUNT_UNKNOWN"}
            cash = [
                self.accounts.cash(account.account_id, account.currency),
            ]
            snap = self.portfolio.snapshot(account.account_id, cash)
            self.audit.record("portfolio.snapshot", {
                "snapshot_id": snap.snapshot_id,
                "account_id": account_id,
            })
            return "snapshot", {"ok": True, "snapshot": snap.to_dict()}

        # ---------------- mutual-fund ----------------
        if command == "investment-mobile-fund-import-nav":
            return "fund", self.fund.import_nav(payload)

        if command == "investment-mobile-fund-import-txn":
            result = self.fund.import_transaction(payload)
            if result.get("ok"):
                self.audit.record("fund.txn", result["transaction"])
            return "fund", result

        if command == "investment-mobile-fund-units":
            return "fund", {
                "ok": True,
                "units": self.fund.units_held(str(payload.get("instrument_id") or "")),
                "transactions": self.fund.transactions(
                    payload.get("instrument_id")
                ),
            }

        # ---------------- backtest + audit ----------------
        if command == "investment-mobile-backtest":
            return "backtest", self.backtest_engine.run(payload)

        if command == "investment-mobile-audit-tail":
            return "audit", {
                "ok": True,
                "events": self.audit.tail(payload.get("limit", 50)),
            }

        raise PermissionError("PERMISSION_DENIED")

    # ------------------------------------------------------------------
    @staticmethod
    def _proposal(payload: dict[str, Any]) -> TradeProposal | None:
        proposal = TradeProposal(
            instrument_id=str(payload.get("instrument_id") or ""),
            market=str(payload.get("market") or ""),
            side=str(payload.get("side") or OrderSide.BUY.value),
            quantity=float(payload.get("quantity") or 0.0),
            price=payload.get("price"),
            strategy_id=str(payload.get("strategy_id") or "manual"),
            signal_id=str(payload.get("signal_id") or ""),
        )
        if not proposal.instrument_id or proposal.quantity <= 0:
            return None
        return proposal
