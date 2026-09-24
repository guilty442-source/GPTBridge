"""Trading engine service — governed command surface for the 11 domains.

Commands are own-tool / governance-actor commands on the
``investment-mobile-*`` surface. Business requests keep relaying through
``xingcheng → ai-assistant``; the engines never touch models, networks,
or the business database directly.
"""

from __future__ import annotations

import time
from datetime import date as _date_t
from decimal import Decimal
from pathlib import Path
from typing import Any


def _d_today() -> "_date_t":
    return _date_t.today()

from .accounts import AccountRegistry
from .ai_boundary import SignalIntake
from .audit import TradingAudit
from .backtest import (
    BacktestComparisonService,
    BacktestConfig,
    BacktestEngine,
    BacktestJobQueue,
    BacktestMaintenance,
    ExecutionSimulationEngine,
    FundBacktestEngine,
    HistoricalUniverseService,
    PortfolioBacktestEngine,
    TradingCostEngine,
)
from .broker.base import BrokerRegistry
from .contracts import OrderSide, TradeProposal
from .domains import DOMAINS
from .fund import (
    FundMaintenance,
    FundNAV,
    FundRecommendation,
    FundTransaction,
    FundTxnStatus,
    MutualFundEngine,
    NavType,
)
from .instruments import InstrumentRegistry
from .intelligence import (
    AnalysisEvidence,
    AnalysisTaskKind,
    EvidenceKind,
    IntelligenceMaintenance,
    InvestmentIntelligenceEngine,
)
from .market import (
    CandleStore,
    CorporateAction,
    CorporateActionService,
    CurrencyRateService,
    HistoricalMarketDataService,
    ManualImportSource,
    MarketDataEngine,
    MarketDataMaintenance,
    MarketDataQuery,
    MarketDataSource,
    SimulatedSource,
    TradingCalendar,
)
from .modes import ModeGate
from .oms import OrderManagementSystem
from .portfolio_engine import PortfolioEngine
from .risk_engine import RiskEngine
from .strategy import (
    AIStrategyResearchService,
    MarketKind,
    StrategyDefinition,
    StrategyEvaluationService,
    StrategyRegistry,
    StrategyValidationService,
    StrategyVersionService,
)
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
            "investment-mobile-market-subscribe",
            "investment-mobile-market-unsubscribe",
            "investment-mobile-market-status",
            "investment-mobile-market-capabilities",
            "investment-mobile-market-quote",
            "investment-mobile-market-fresh-price",
            "investment-mobile-market-history-sync",
            "investment-mobile-market-candles",
            "investment-mobile-market-gaps",
            "investment-mobile-market-maintenance",
            "investment-mobile-calendar-check",
            "investment-mobile-calendar-update",
            "investment-mobile-corporate-action",
            "investment-mobile-corporate-adjust",
            "investment-mobile-fx-rate-record",
            "investment-mobile-fx-convert",
            "investment-mobile-fx-history",
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
            "investment-mobile-fund-register",
            "investment-mobile-fund-classify",
            "investment-mobile-fund-list",
            "investment-mobile-fund-providers",
            "investment-mobile-fund-nav-file",
            "investment-mobile-fund-nav-record",
            "investment-mobile-fund-nav-latest",
            "investment-mobile-fund-nav-history",
            "investment-mobile-fund-distribution-record",
            "investment-mobile-fund-distributions",
            "investment-mobile-fund-fee-register",
            "investment-mobile-fund-fee-quote",
            "investment-mobile-fund-fee-compare",
            "investment-mobile-fund-performance",
            "investment-mobile-fund-risk-metrics",
            "investment-mobile-fund-compare",
            "investment-mobile-fund-holdings-import",
            "investment-mobile-fund-holdings",
            "investment-mobile-fund-exposure",
            "investment-mobile-fund-overlap",
            "investment-mobile-fund-txn-create",
            "investment-mobile-fund-txn-transition",
            "investment-mobile-fund-txn-list",
            "investment-mobile-fund-txn-pending",
            "investment-mobile-fund-cost-basis",
            "investment-mobile-fund-plan-register",
            "investment-mobile-fund-plan-analyze",
            "investment-mobile-fund-plan-status",
            "investment-mobile-fund-recommend",
            "investment-mobile-fund-recommendations",
            "investment-mobile-fund-strategy-register",
            "investment-mobile-fund-strategies",
            "investment-mobile-fund-unified-exposure",
            "investment-mobile-fund-maintenance",
            "investment-mobile-fund-status",
            # backtest + audit domains
            "investment-mobile-backtest",
            "investment-mobile-strategy-register",
            "investment-mobile-strategy-list",
            "investment-mobile-strategy-transition",
            "investment-mobile-strategy-disable",
            "investment-mobile-strategy-snapshot",
            "investment-mobile-strategy-versions",
            "investment-mobile-strategy-validate",
            "investment-mobile-strategy-stability",
            "investment-mobile-strategy-search-log",
            "investment-mobile-strategy-assess",
            "investment-mobile-strategy-research",
            "investment-mobile-strategy-propose",
            "investment-mobile-backtest-results",
            "investment-mobile-backtest-compare",
            "investment-mobile-backtest-submit",
            "investment-mobile-backtest-job",
            "investment-mobile-backtest-cancel",
            "investment-mobile-backtest-maintenance",
            "investment-mobile-fee-rules",
            "investment-mobile-fee-rule-add",
            "investment-mobile-universe-register",
            "investment-mobile-universe-members",
            "investment-mobile-broker-capability",
            "investment-mobile-audit-tail",
            "investment-mobile-domains",
            # ai-intelligence domain (星澄決策中心 — advisory only)
            "investment-mobile-ai-status",
            "investment-mobile-ai-analyze",
            "investment-mobile-ai-analyze-tw",
            "investment-mobile-ai-analyze-us",
            "investment-mobile-ai-analyze-fund",
            "investment-mobile-ai-analyze-portfolio",
            "investment-mobile-ai-intent",
            "investment-mobile-ai-propose",
            "investment-mobile-ai-proposal-status",
            "investment-mobile-ai-recommend",
            "investment-mobile-ai-rec-transition",
            "investment-mobile-ai-rec-list",
            "investment-mobile-ai-rec-versions",
            "investment-mobile-ai-rec-expire",
            "investment-mobile-ai-outcome",
            "investment-mobile-ai-outcomes",
            "investment-mobile-ai-schedules",
            "investment-mobile-ai-schedule-run",
            "investment-mobile-ai-schedule-due",
            "investment-mobile-ai-model-health",
            "investment-mobile-ai-boundary",
            "investment-mobile-ai-inspect-text",
            "investment-mobile-ai-maintenance",
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
        # market-data domain stack
        self.calendar = TradingCalendar(state_dir)
        self.market_engine = MarketDataEngine(state_dir, audit=self.audit)
        self.candle_store = CandleStore(
            Path(tool_root) / "runtime" / "data" / "market-data.sqlite3"
        )
        self.history = HistoricalMarketDataService(self.candle_store, self.calendar)
        self.corporate = CorporateActionService(state_dir)
        self.fx = CurrencyRateService(state_dir)
        self.market_query = MarketDataQuery(
            self.market_engine, self.candle_store, self.calendar, self.corporate
        )
        self.market_maintenance = MarketDataMaintenance(
            self.market_engine, self.candle_store, self.history
        )
        self.manual_source = ManualImportSource()
        self.simulated_source = SimulatedSource()
        self.market_engine.register_source(self.manual_source)
        self.market_engine.register_source(self.simulated_source)
        self.market_data_sources: dict[str, MarketDataSource] = {
            self.manual_source.capability.source_id: self.manual_source,
            self.simulated_source.capability.source_id: self.simulated_source,
        }
        # business-layer mirror: bounded outbox drained through the
        # governed channel (investment-mobile -> xingcheng -> ai-assistant)
        from collections import deque
        self._mirror_outbox: deque[dict[str, Any]] = deque(maxlen=2000)
        self._channel_client: Any | None = None
        self.market_engine.on_flush(
            lambda batch: self._mirror_outbox.extend(
                {"operation": "record_market_quote", "quote": q}
                for q in batch
            )
        )
        self.fund_engine = MutualFundEngine(state_dir, self.fx)
        self.fund_maintenance = FundMaintenance(self.fund_engine)
        # 星澄 AI 投資決策中心 — advisory only; proposals re-enter the
        # governed strategy→risk→OMS pipeline, never bypass it.
        self.intel = InvestmentIntelligenceEngine(
            state_dir, self.market_engine, self.fund_engine,
            self.portfolio, self.accounts, self.fx, self.calendar,
            consult=self._ai_consult, candle_store=self.candle_store,
        )
        self.intel.router._probe = lambda: self._channel_client is not None
        self.intel_maintenance = IntelligenceMaintenance(
            self.intel.router, self.intel.lifecycle, self.intel.scheduler)
        self.brokers = BrokerRegistry(state_dir)
        self.ai_intake = SignalIntake(self.strategy)
        # strategy lifecycle + backtest cluster — simulation only; no
        # path from these engines reaches OMS/broker/account writers
        self.strategy_registry = StrategyRegistry(state_dir)
        self.strategy_versions = StrategyVersionService(state_dir)
        self.strategy_validation = StrategyValidationService(state_dir)
        self.strategy_evaluator = StrategyEvaluationService()
        self.strategy_research = AIStrategyResearchService(
            self.intel.router, self.strategy_registry)
        self.bt_cost = TradingCostEngine(state_dir)
        self.bt_universe = HistoricalUniverseService(state_dir)
        self.bt_exec = ExecutionSimulationEngine()
        self.backtest_engine = BacktestEngine(
            state_dir, self.candle_store, cost_engine=self.bt_cost,
            universe=self.bt_universe, exec_engine=self.bt_exec)
        self.fund_backtest = FundBacktestEngine(self.fund_engine)
        self.portfolio_backtest = PortfolioBacktestEngine(self.fx)
        self.bt_compare = BacktestComparisonService()
        self.bt_queue = BacktestJobQueue(state_dir)
        self.bt_maintenance = BacktestMaintenance(
            state_dir,
            revision_feed=lambda: sorted({
                r["instrument_id"]
                for r in self.candle_store.revisions()}))
        self.oms = OrderManagementSystem(
            state_dir,
            mode_gate=self.mode_gate,
            risk=self.risk,
            portfolio=self.portfolio,
            accounts=self.accounts,
            brokers=self.brokers,
            audit=self.audit,
            market=self.market_engine,
        )

    # ------------------------------------------------------------------
    def owns(self, command: str) -> bool:
        return str(command) in self.COMMANDS

    def bind_channel(self, channel: Any | None) -> None:
        self.audit.bind_channel(channel)

    def bind_client(self, client: Any | None) -> None:
        """Bind the governed ChannelClient used for business-layer mirrors."""
        self._channel_client = client

    async def _drain_mirror(self, budget: int = 100) -> None:
        """Forward queued mirror records to ai-assistant — bounded per call."""
        if self._channel_client is None:
            return
        sent = 0
        while self._mirror_outbox and sent < budget:
            op = self._mirror_outbox.popleft()
            try:
                result = await self._channel_client.submit_instruction(op)
            except Exception:
                result = {"ok": False}
            sent += 1
            if result.get("ok") is False:
                self._mirror_outbox.appendleft(op)  # retry next drain
                return

    async def _ai_consult(self, prompt: str, source: str) -> dict[str, Any]:
        """Governed 星澄 consult through the ai channel (submit-only)."""
        if self._channel_client is None:
            return {"ok": False}
        try:
            res = await self._channel_client.submit_instruction({
                "operation": "analysis", "instruction": prompt})
        except Exception:
            return {"ok": False}
        inner = res.get("analysis") if isinstance(res.get("analysis"), dict) else {}
        return {
            "ok": res.get("ok") is not False and inner.get("ok") is not False,
            "response": inner.get("response") or inner.get("text") or "",
            "model_version": inner.get("model_version") or "",
        }

    # ------------------------------------------------------------------
    async def handle(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if not self.owns(command):
            raise PermissionError("PERMISSION_DENIED")
        payload = dict(payload or {})
        await self._drain_mirror()

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
            payload = dict(payload)
            iid = str(payload.get("instrument_id") or "")
            # derive market/currency from the collision-free id when omitted
            if not payload.get("market") and ":" in iid:
                payload["market"] = iid.split(":", 1)[0]
            if not payload.get("currency") and ":" in iid:
                payload["currency"] = iid.rsplit(":", 1)[-1]
            quote = self.manual_source.submit_quote(payload)
            return "market", self.market_engine.ingest_quote(quote)

        if command == "investment-mobile-market-latest":
            return "market", {
                "ok": True,
                "observation": self.market_engine.latest_quote(
                    str(payload.get("instrument_id") or "")
                ),
            }

        if command == "investment-mobile-market-subscribe":
            return "market", self.market_engine.subscribe(
                str(payload.get("consumer_id") or "operator"),
                list(payload.get("instrument_ids") or []),
            )

        if command == "investment-mobile-market-unsubscribe":
            return "market", self.market_engine.unsubscribe(
                str(payload.get("consumer_id") or "operator"),
                list(payload.get("instrument_ids") or []),
            )

        if command == "investment-mobile-market-status":
            return "market-status", self.market_engine.status()

        if command == "investment-mobile-market-capabilities":
            return "market-capabilities", {
                "ok": True,
                "sources": self.market_engine.capability_matrix(),
            }

        if command == "investment-mobile-market-quote":
            return "market", self.market_query.quote(
                str(payload.get("instrument_id") or "")
            )

        if command == "investment-mobile-market-fresh-price":
            return "market", self.market_engine.fresh_price(
                str(payload.get("instrument_id") or ""),
                float(payload.get("max_age_s") or 30.0),
            )

        if command == "investment-mobile-market-history-sync":
            source = self.market_data_sources.get(str(payload.get("source_id") or ""))
            if source is None:
                return "market", {"ok": False, "error_code": "SOURCE_UNKNOWN"}
            result = await self.history.sync(
                source,
                str(payload.get("instrument_id") or ""),
                str(payload.get("timeframe") or "1d"),
                self._dt(payload.get("start"))
                or self._dt("2000-01-01T00:00:00+00:00"),
                self._dt(payload.get("end")) or self._dt(None),
            )
            if result.get("ok"):
                for c in self.candle_store.candles(
                    str(payload.get("instrument_id") or ""),
                    str(payload.get("timeframe") or "1d"),
                    self._dt(payload.get("start")),
                    self._dt(payload.get("end")),
                ):
                    self._mirror_outbox.append({
                        "operation": "record_market_candle",
                        "candle": c.to_dict(),
                    })
            return "market", result

        if command == "investment-mobile-market-candles":
            return "market", self.market_query.history(
                str(payload.get("instrument_id") or ""),
                timeframe=str(payload.get("timeframe") or "1d"),
                start=self._dt(payload.get("start")),
                end=self._dt(payload.get("end")),
                adjustment_type=str(payload.get("adjustment_type") or "raw"),
                limit=int(payload.get("limit") or 500),
            )

        if command == "investment-mobile-market-gaps":
            return "market", {
                "ok": True,
                "gaps": self.history.gap_check(
                    str(payload.get("instrument_id") or ""),
                    str(payload.get("market") or "tw"),
                    str(payload.get("timeframe") or "1d"),
                    self._dt(payload.get("start"))
                    or self._dt("2000-01-01T00:00:00+00:00"),
                    self._dt(payload.get("end")) or self._dt(None),
                ),
            }

        if command == "investment-mobile-market-maintenance":
            report = self.market_maintenance.run_once()
            for st in report["sources"]:
                self._mirror_outbox.append({
                    "operation": "record_market_status", "status": st,
                })
            return "market-maintenance", report

        if command == "investment-mobile-calendar-check":
            return "calendar", self.market_query.market_session(
                str(payload.get("market") or "tw"),
                at=self._dt(payload.get("at")) if payload.get("at") else None,
            )

        if command == "investment-mobile-calendar-update":
            from datetime import date as _date
            upd = self.calendar.update_day(
                str(payload.get("market") or ""),
                _date.fromisoformat(str(payload.get("day") or "")),
                bool(payload.get("closed")),
                str(payload.get("reason") or ""),
            )
            self.audit.record("market.calendar_update", {
                "market": upd.market, "day": upd.day.isoformat(),
                "closed": upd.closed,
            })
            return "calendar", {"ok": True, "update": upd.day.isoformat()}

        if command == "investment-mobile-corporate-action":
            from datetime import date as _date
            from decimal import Decimal as _D
            action = CorporateAction(
                instrument_id=str(payload.get("instrument_id") or ""),
                kind=str(payload.get("kind") or ""),
                effective_date=_date.fromisoformat(str(payload.get("effective_date") or "")),
                source_id=str(payload.get("source_id") or "operator"),
                ratio=_D(str(payload.get("ratio") or "1")),
                cash_amount=_D(str(payload.get("cash_amount") or "0")),
            )
            result = self.corporate.record(action)
            self.audit.record("market.corporate_action", result["action"])
            return "corporate", result

        if command == "investment-mobile-corporate-adjust":
            candles = self.candle_store.candles(
                str(payload.get("instrument_id") or ""),
                str(payload.get("timeframe") or "1d"),
                adjustment_type="raw",
            )
            from datetime import date as _date
            as_of = (
                _date.fromisoformat(str(payload["as_of"]))
                if payload.get("as_of") else None
            )
            adjusted = self.corporate.adjust(
                candles, str(payload.get("adjustment_type") or "raw"), as_of=as_of
            )
            return "corporate", {
                "ok": True,
                "adjustment_type": str(payload.get("adjustment_type") or "raw"),
                "candles": [c.to_dict() for c in adjusted],
            }

        if command == "investment-mobile-fx-rate-record":
            return "fx", self.fx.record_rate(
                str(payload.get("base") or ""),
                str(payload.get("quote") or ""),
                payload.get("rate") or "0",
                str(payload.get("source_id") or "manual"),
            )

        if command == "investment-mobile-fx-convert":
            return "fx", self.fx.convert(
                payload.get("amount") or "0",
                str(payload.get("from_currency") or ""),
                str(payload.get("to_currency") or ""),
            )

        if command == "investment-mobile-fx-history":
            return "fx", {
                "ok": True,
                "rates": self.fx.history(
                    str(payload.get("base") or ""), str(payload.get("quote") or "")
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
        if command == "investment-mobile-fund-register":
            result = self.fund_engine.identities.register(payload)
            if result.get("ok"):
                self.audit.record("fund.identity", result["identity"])
            return "fund", result

        if command == "investment-mobile-fund-classify":
            return "fund", self.fund_engine.identities.classify(payload)

        if command == "investment-mobile-fund-list":
            return "fund", {
                "ok": True,
                "funds": self.fund_engine.identities.list(
                    payload.get("fund_id")),
            }

        if command == "investment-mobile-fund-providers":
            return "fund-providers", {
                "ok": True,
                "providers": self.fund_engine.capability_matrix(),
            }

        if command == "investment-mobile-fund-nav-file":
            result = self.fund_engine.nav.import_file(
                Path(str(payload.get("path") or "")),
                str(payload.get("source_id") or "manual-import"),
            )
            for nav in result.get("imported_navs") or []:
                self._mirror_outbox.append({
                    "operation": "record_fund_nav",
                    "nav": nav if isinstance(nav, dict) else nav.to_dict()})
            return "fund", result

        if command == "investment-mobile-fund-nav-record":
            nav = FundNAV(
                fund_id=str(payload.get("fund_id") or ""),
                share_class_id=str(payload.get("share_class_id") or ""),
                nav_date=payload.get("nav_date"),
                nav=payload.get("nav") or "0",
                currency=str(payload.get("currency") or ""),
                source_id=str(payload.get("source_id") or "manual-import"),
                nav_type=str(payload.get("nav_type") or NavType.PUBLISHED.value),
                published_at=self._dt(payload.get("published_at")),
                revision=int(payload.get("revision") or 1),
            )
            result = self.fund_engine.nav.record(nav)
            if result.get("ok"):
                self._mirror_outbox.append({
                    "operation": "record_fund_nav", "nav": nav.to_dict()})
            return "fund", result

        if command == "investment-mobile-fund-nav-latest":
            return "fund", self.fund_engine.nav.latest_published(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
            )

        if command == "investment-mobile-fund-nav-history":
            rows = self.fund_engine.nav.history(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
                nav_type=str(payload.get("nav_type") or NavType.PUBLISHED.value),
            )
            return "fund", {"ok": True, "navs": [n.to_dict() for n in rows]}

        if command == "investment-mobile-fund-distribution-record":
            from .fund.contracts import FundDistribution
            dist = FundDistribution(
                fund_id=str(payload.get("fund_id") or ""),
                share_class_id=str(payload.get("share_class_id") or ""),
                ex_distribution_date=payload.get("ex_distribution_date"),
                payment_date=payload.get("payment_date"),
                amount_per_unit=payload.get("amount_per_unit") or "0",
                currency=str(payload.get("currency") or ""),
                distribution_source=str(
                    payload.get("distribution_source") or "unconfirmed"),
                frequency=str(payload.get("frequency") or ""),
                source_id=str(payload.get("source_id") or "manual-import"),
            )
            result = self.fund_engine.distributions.record(dist)
            if result.get("ok"):
                self._mirror_outbox.append({
                    "operation": "record_fund_distribution",
                    "distribution": dist.to_dict()})
            return "fund", result

        if command == "investment-mobile-fund-distributions":
            return "fund", {
                "ok": True,
                "distributions": self.fund_engine.distributions.list(
                    payload.get("share_class_id"), payload.get("fund_id")),
                "totals": self.fund_engine.distributions.totals(
                    str(payload.get("share_class_id") or "")),
            }

        if command == "investment-mobile-fund-fee-register":
            from .fund.contracts import FundFee
            fee = FundFee(
                fund_id=str(payload.get("fund_id") or ""),
                share_class_id=str(payload.get("share_class_id") or ""),
                kind=str(payload.get("kind") or ""),
                calc=str(payload.get("calc") or "percent"),
                rate=payload.get("rate") or "0",
                currency=str(payload.get("currency") or ""),
                platform=str(payload.get("platform") or ""),
                min_holding_days=payload.get("min_holding_days"),
                tiers=list(payload.get("tiers") or []),
                effective_from=str(payload.get("effective_from") or ""),
                effective_to=str(payload.get("effective_to") or ""),
                source_id=str(payload.get("source_id") or "operator"),
            )
            return "fund", self.fund_engine.fees.register(fee)

        if command == "investment-mobile-fund-fee-quote":
            return "fund", self.fund_engine.fees.transaction_fees(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
                tuple(payload.get("kinds") or ()),
                payload.get("amount") or "0",
                platform=str(payload.get("platform") or ""),
                holding_days=payload.get("holding_days"),
                currency=str(payload.get("currency") or ""),
            )

        if command == "investment-mobile-fund-fee-compare":
            return "fund", self.fund_engine.fees.compare(
                [tuple(t) for t in payload.get("targets") or []],
                payload.get("amount") or "100000",
                platform=str(payload.get("platform") or ""),
                holding_days=payload.get("holding_days"),
            )

        if command == "investment-mobile-fund-performance":
            return "fund", self.fund_engine.performance.all_periods(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
                bool(payload.get("include_distributions")),
            )

        if command == "investment-mobile-fund-risk-metrics":
            return "fund", self.fund_engine.performance.risk_metrics(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
                include_distributions=bool(
                    payload.get("include_distributions")),
            )

        if command == "investment-mobile-fund-compare":
            return "fund", self.fund_engine.comparison.compare(
                [tuple(t) for t in payload.get("targets") or []],
                period=str(payload.get("period") or "1y"),
                base_currency=str(payload.get("base_currency") or "TWD"),
                include_distributions=bool(
                    payload.get("include_distributions")),
                include_fees=bool(payload.get("include_fees")),
                benchmark=(
                    tuple(payload["benchmark"])
                    if payload.get("benchmark") else None
                ),
            )

        if command == "investment-mobile-fund-holdings-import":
            from datetime import date as _date
            result = self.fund_engine.exposure.import_holdings(
                str(payload.get("fund_id") or ""),
                list(payload.get("holdings") or []),
                str(payload.get("source_id") or "manual-import"),
                _date.fromisoformat(str(payload["as_of_date"]))
                if payload.get("as_of_date") else None,
            )
            self.audit.record("fund.holdings_import", {
                "fund_id": payload.get("fund_id"),
                "imported": result.get("imported"),
            })
            return "fund", result

        if command == "investment-mobile-fund-holdings":
            return "fund", self.fund_engine.exposure.holdings(
                str(payload.get("fund_id") or ""))

        if command == "investment-mobile-fund-exposure":
            return "fund", self.fund_engine.exposure.breakdown(
                str(payload.get("fund_id") or ""),
                str(payload.get("by") or "asset_kind"),
            )

        if command == "investment-mobile-fund-overlap":
            return "fund", self.fund_engine.exposure.overlap(
                str(payload.get("fund_a") or ""),
                str(payload.get("fund_b") or ""),
            )

        if command == "investment-mobile-fund-txn-create":
            txn = self._fund_txn(payload)
            result = self.fund_engine.transactions.create(txn)
            if result.get("ok"):
                self.audit.record("fund.txn_created",
                                  result["transaction"])
            return "fund", result

        if command == "investment-mobile-fund-txn-transition":
            result = self.fund_engine.transactions.transition(
                str(payload.get("transaction_id") or ""),
                str(payload.get("target") or ""),
                confirmed_nav=payload.get("confirmed_nav"),
                pricing_date=payload.get("pricing_date"),
                settlement_date=payload.get("settlement_date"),
            )
            if result.get("ok"):
                self.audit.record("fund.txn_transition", {
                    "transaction_id": payload.get("transaction_id"),
                    "status": result["transaction"]["status"],
                })
                self._mirror_outbox.append({
                    "operation": "record_fund_transaction",
                    "transaction": result["transaction"]})
            return "fund", result

        if command == "investment-mobile-fund-txn-list":
            return "fund", {
                "ok": True,
                "transactions": self.fund_engine.transactions.list(
                    payload.get("account_id")),
            }

        if command == "investment-mobile-fund-txn-pending":
            return "fund", {
                "ok": True,
                "pending": self.fund_engine.transactions.pending_settlement(
                    payload.get("account_id")),
                "note": "贖回款未入帳前不得增加可交易現金",
            }

        if command == "investment-mobile-fund-cost-basis":
            return "fund", self.fund_engine.cost_basis.basis(
                str(payload.get("account_id") or ""),
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
            )

        if command == "investment-mobile-fund-plan-register":
            return "fund", self.fund_engine.recurring.register_plan(payload)

        if command == "investment-mobile-fund-plan-analyze":
            return "fund", self.fund_engine.recurring.analyze(
                str(payload.get("plan_id") or ""))

        if command == "investment-mobile-fund-plan-status":
            return "fund", self.fund_engine.recurring.set_status(
                str(payload.get("plan_id") or ""),
                str(payload.get("status") or ""),
            )

        if command == "investment-mobile-fund-recommend":
            from datetime import date as _date
            rec = FundRecommendation(
                fund_id=str(payload.get("fund_id") or ""),
                share_class_id=str(payload.get("share_class_id") or ""),
                account_id=str(payload.get("account_id") or ""),
                recommendation_type=str(
                    payload.get("recommendation_type") or ""),
                analysis_date=_date.fromisoformat(
                    str(payload.get("analysis_date") or
                        _date.today().isoformat())),
                nav_date=(_date.fromisoformat(str(payload["nav_date"]))
                          if payload.get("nav_date") else None),
                reasoning=str(payload.get("reasoning") or ""),
                data_sources=list(payload.get("data_sources") or []),
                investment_horizon=str(
                    payload.get("investment_horizon") or ""),
                risk_factors=list(payload.get("risk_factors") or []),
                suggested_amount=payload.get("suggested_amount"),
                suggested_weight=payload.get("suggested_weight"),
                reevaluate_when=list(payload.get("reevaluate_when") or []),
                model_id=str(payload.get("model_id") or ""),
                model_version=str(payload.get("model_version") or ""),
                strategy_version=str(payload.get("strategy_version") or ""),
            )
            result = self.fund_engine.recommendations.record(rec)
            if result.get("ok"):
                self.audit.record("fund.recommendation",
                                  result["recommendation"])
                self._mirror_outbox.append({
                    "operation": "record_fund_recommendation",
                    "recommendation": result["recommendation"]})
            return "fund", result

        if command == "investment-mobile-fund-recommendations":
            return "fund", {
                "ok": True,
                "recommendations": self.fund_engine.recommendations.list(
                    payload.get("fund_id"), int(payload.get("limit") or 100)),
            }

        if command == "investment-mobile-fund-strategy-register":
            from .fund.contracts import FundStrategy
            s = FundStrategy(
                strategy_id=str(payload.get("strategy_id") or ""),
                strategy_version=str(payload.get("strategy_version") or "1"),
                kind=str(payload.get("kind") or "allocation"),
                parameters=dict(payload.get("parameters") or {}),
            )
            return "fund", self.fund_engine.strategies.register(s)

        if command == "investment-mobile-fund-strategies":
            return "fund", {
                "ok": True,
                "strategies": self.fund_engine.strategies.active(
                    payload.get("strategy_id")),
            }

        if command == "investment-mobile-fund-unified-exposure":
            return "fund", self.fund_engine.unified.analyze(
                list(payload.get("direct_positions") or []),
                list(payload.get("fund_positions") or []),
                list(payload.get("cash") or []),
            )

        if command == "investment-mobile-fund-maintenance":
            return "fund-maintenance", self.fund_maintenance.run_once()

        if command == "investment-mobile-fund-status":
            return "fund-status", self.fund_engine.status()

        # legacy manual-import commands → new services (compat surface)
        if command == "investment-mobile-fund-import-nav":
            iid = str(payload.get("instrument_id") or "")
            if not iid.startswith("fund:"):
                return "fund", {"ok": False, "error_code": "INVALID_FUND_NAV"}
            parts = iid.split(":")
            try:
                nav = FundNAV(
                    fund_id=parts[1],
                    share_class_id=parts[2] if len(parts) > 2 else "NA",
                    nav_date=payload.get("nav_date")
                             or _d_today(),
                    nav=payload.get("nav") or "0",
                    currency=str(payload.get("currency")
                                 or (parts[3] if len(parts) > 3 else "")),
                    source_id=str(payload.get("source") or "manual-import"),
                )
            except (ValueError, TypeError):
                return "fund", {"ok": False, "error_code": "INVALID_FUND_NAV"}
            if nav.nav <= 0:
                return "fund", {"ok": False, "error_code": "INVALID_FUND_NAV"}
            result = self.fund_engine.nav.record(nav)
            if result.get("ok"):
                self._mirror_outbox.append({
                    "operation": "record_fund_nav", "nav": nav.to_dict()})
            return "fund", result

        if command == "investment-mobile-fund-import-txn":
            iid = str(payload.get("instrument_id") or "")
            parts = iid.split(":")
            kind = {"subscription": "subscribe",
                    "redemption": "redeem"}.get(
                str(payload.get("kind") or payload.get("transaction_type") or ""),
                str(payload.get("transaction_type") or ""),
            )
            units = Decimal(str(payload.get("units") or 0))
            amount = Decimal(str(payload.get("amount") or 0))
            nav = payload.get("confirmed_nav") or (
                str(amount / units) if units > 0 else None
            )
            txn = FundTransaction(
                account_id=str(payload.get("account_id")
                               or "fund-provider-generic"),
                fund_id=parts[1] if len(parts) > 1 else iid,
                share_class_id=parts[2] if len(parts) > 2 else "NA",
                transaction_type=kind,
                amount=str(amount), units=str(units),
                confirmed_nav=nav,
                currency=str(payload.get("currency")
                             or (parts[3] if len(parts) > 3 else "")),
                source_id=str(payload.get("source") or "manual-import"),
            )
            result = self.fund_engine.transactions.import_settled(txn)
            if result.get("ok"):
                self.audit.record("fund.txn", result["transaction"])
                self._mirror_outbox.append({
                    "operation": "record_fund_transaction",
                    "transaction": result["transaction"]})
            return "fund", result

        if command == "investment-mobile-fund-units":
            iid = str(payload.get("instrument_id") or "")
            parts = iid.split(":")
            basis = self.fund_engine.cost_basis.basis(
                str(payload.get("account_id") or "fund-provider-generic"),
                parts[1] if len(parts) > 1 else iid,
                parts[2] if len(parts) > 2 else "NA",
            )
            return "fund", {
                "ok": True,
                "units": float(basis["units"]),
                "basis": basis,
            }

        # ---------------- 星澄 AI 投資決策中心 (advisory only) ----------------
        if command == "investment-mobile-ai-status":
            return "ai-intel", self.intel.status()

        if command == "investment-mobile-ai-intent":
            intent = self.intel.intent.parse(str(payload.get("text") or ""))
            return "ai-intel", {"ok": True, "intent": intent.to_dict()}

        if command == "investment-mobile-ai-analyze-tw":
            res = await self.intel.tw.analyze(
                str(payload.get("instrument_id") or ""),
                benchmark_id=payload.get("benchmark_id"))
            self.audit.record("ai.analyze", {
                "instrument_id": payload.get("instrument_id"),
                "degraded": res.get("degraded")})
            return "ai-intel", res

        if command == "investment-mobile-ai-analyze-us":
            res = await self.intel.us.analyze(
                str(payload.get("instrument_id") or ""),
                benchmark_id=payload.get("benchmark_id"))
            res["macro"] = self.intel.us.macro_block()
            self.audit.record("ai.analyze", {
                "instrument_id": payload.get("instrument_id"),
                "degraded": res.get("degraded")})
            return "ai-intel", res

        if command == "investment-mobile-ai-analyze-fund":
            res = await self.intel.fund_intel.analyze(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or ""),
                account_id=str(payload.get("account_id") or ""),
                target_weight=payload.get("target_weight"),
            )
            self.audit.record("ai.analyze_fund", {
                "fund_id": payload.get("fund_id"),
                "degraded": res.get("degraded")})
            return "ai-intel", res

        if command == "investment-mobile-ai-analyze-portfolio":
            res = await self.intel.portfolio_intel.analyze(
                base_currency=str(payload.get("base_currency") or "TWD"),
                account_ids=list(payload.get("account_ids") or []) or None,
            )
            self.audit.record("ai.analyze_portfolio", {
                "degraded": res.get("degraded")})
            return "ai-intel", res

        if command == "investment-mobile-ai-analyze":
            # intent-routed generic analysis
            intent = self.intel.intent.parse(str(payload.get("text") or ""))
            if intent.ambiguous:
                return "ai-intel", {
                    "ok": False, "error_code": "AMBIGUOUS_INSTRUMENT",
                    "intent": intent.to_dict(),
                    "note": "模糊商品名稱需先確認識別（例：台積電 vs TSM）"}
            if intent.market == "tw" and intent.instrument_id:
                return "ai-intel", await self.intel.tw.analyze(
                    intent.instrument_id)
            if intent.market == "us" and intent.instrument_id:
                res = await self.intel.us.analyze(intent.instrument_id)
                res["macro"] = self.intel.us.macro_block()
                return "ai-intel", res
            if intent.market == "fund":
                fid = str(payload.get("fund_id") or "")
                cls = str(payload.get("share_class_id") or "")
                # resolve fund aliases from instrument id if present
                if intent.instrument_id.startswith("fund:"):
                    parts = intent.instrument_id.split(":")
                    fid = fid or (parts[1] if len(parts) > 1 else "")
                    cls = cls or (parts[2] if len(parts) > 2 else "")
                if fid:
                    return "ai-intel", await self.intel.fund_intel.analyze(
                        fid, cls,
                        account_id=str(payload.get("account_id") or ""))
            if intent.market == "portfolio":
                return "ai-intel", await self.intel.portfolio_intel.analyze(
                    base_currency=str(payload.get("base_currency") or "TWD"))
            return "ai-intel", {
                "ok": False, "error_code": "INTENT_UNRESOLVED",
                "intent": intent.to_dict()}

        if command == "investment-mobile-ai-propose":
            res = self.intel.proposals.build(payload)
            if res.get("ok"):
                self.audit.record("ai.proposal_validated", {
                    "proposal_id": res["proposal"]["proposal_id"]})
            return "ai-intel", res

        if command == "investment-mobile-ai-proposal-status":
            return "ai-intel", self.intel.proposals.status(
                str(payload.get("proposal_id") or ""))

        if command == "investment-mobile-ai-recommend":
            run = self.intel.pipeline.begin(
                str(payload.get("task_kind") or AnalysisTaskKind.QUICK_MARKET),
                instrument_id=str(payload.get("instrument_id") or ""),
                market=str(payload.get("market") or ""),
                account_id=str(payload.get("account_id") or ""),
                model_id=str(payload.get("model_id") or "xingcheng-native"),
                strategy_id=str(payload.get("strategy_id") or ""),
                strategy_version=str(payload.get("strategy_version") or ""))
            evs = []
            for item in list(payload.get("evidence") or []):
                evs.append(AnalysisEvidence(
                    kind=str(item.get("kind") or
                             EvidenceKind.CALCULATED_RESULT),
                    claim=str(item.get("claim") or ""),
                    value=item.get("value"),
                    source_id=str(item.get("source_id") or ""),
                    data_timestamp=str(item.get("data_timestamp") or ""),
                    computation=str(item.get("computation") or "")))
            self.intel.pipeline.stage(
                run, "recommendation",
                evidence=evs,
                findings=dict(payload.get("findings") or {}))
            self.intel.pipeline.finish(run)
            built = self.intel.pipeline.build_recommendation(
                run,
                account_id=str(payload.get("account_id") or ""),
                instrument_id=str(payload.get("instrument_id") or ""),
                instrument_type=str(payload.get("instrument_type") or "stock"),
                market=str(payload.get("market") or ""),
                recommendation_type=str(
                    payload.get("recommendation_type") or "HOLD"),
                reasoning=str(payload.get("reasoning") or ""),
                risk_factors=list(payload.get("risk_factors") or []),
                reference_price=payload.get("reference_price"),
                reference_nav=payload.get("reference_nav"),
                suggested_weight=payload.get("suggested_weight"),
                observation_window=str(
                    payload.get("observation_window") or ""),
                trigger_conditions=list(
                    payload.get("trigger_conditions") or []),
                reevaluate_conditions=list(
                    payload.get("reevaluate_conditions") or []),
                market_data_timestamp=self._dt(
                    payload.get("market_data_timestamp")),
            )
            if not built.get("ok"):
                return "ai-intel", built
            res = self.intel.lifecycle.create(built["recommendation"])
            if res.get("ok"):
                self.audit.record("ai.recommendation", {
                    "recommendation_id":
                        res["recommendation"]["recommendation_id"],
                    "type": res["recommendation"]["recommendation_type"]})
                self._mirror_outbox.append({
                    "operation": "record_ai_recommendation",
                    "recommendation": res["recommendation"]})
                self._mirror_outbox.append({
                    "operation": "record_analysis_run",
                    "run": run.to_dict()})
            return "ai-intel", res

        if command == "investment-mobile-ai-rec-transition":
            return "ai-intel", self.intel.lifecycle.transition(
                str(payload.get("recommendation_id") or ""),
                str(payload.get("target") or ""),
                reason=str(payload.get("reason") or ""),
                actor=str(payload.get("actor") or ""))

        if command == "investment-mobile-ai-rec-list":
            return "ai-intel", {
                "ok": True,
                "recommendations": self.intel.lifecycle.list(
                    status=payload.get("status"),
                    instrument_id=payload.get("instrument_id"),
                    limit=int(payload.get("limit") or 200)),
            }

        if command == "investment-mobile-ai-rec-versions":
            return "ai-intel", {
                "ok": True,
                "versions": self.intel.lifecycle.versions(
                    str(payload.get("recommendation_id") or "")),
            }

        if command == "investment-mobile-ai-rec-expire":
            return "ai-intel", self.intel.lifecycle.expire_due()

        if command == "investment-mobile-ai-outcome":
            return "ai-intel", self.intel.outcomes.evaluate(
                str(payload.get("recommendation_id") or ""),
                horizon_days=int(payload.get("horizon_days") or 30),
                kind=str(payload.get("kind") or "ai_analysis"))

        if command == "investment-mobile-ai-outcomes":
            return "ai-intel", {
                "ok": True,
                "outcomes": self.intel.outcomes.outcomes(
                    payload.get("recommendation_id"),
                    payload.get("kind")),
            }

        if command == "investment-mobile-ai-schedules":
            return "ai-intel", {
                "ok": True, "schedules": self.intel.scheduler.list()}

        if command == "investment-mobile-ai-schedule-due":
            return "ai-intel", {
                "ok": True,
                "due": self.intel.scheduler.due_slots(
                    market_date_fresh=dict(
                        payload.get("market_date_fresh") or {})),
            }

        if command == "investment-mobile-ai-schedule-run":
            return "ai-intel", self.intel.scheduler.mark_ran(
                str(payload.get("schedule_id") or ""),
                str(payload.get("market_date") or ""),
            )

        if command == "investment-mobile-ai-model-health":
            return "ai-intel", {
                "ok": True, "router": self.intel.router.health(),
                "records": self.intel.router.records(
                    int(payload.get("limit") or 50))}

        if command == "investment-mobile-ai-boundary":
            return "ai-intel", self.intel.safety.boundary_manifest()

        if command == "investment-mobile-ai-inspect-text":
            return "ai-intel", self.intel.safety.inspect_external_text(
                str(payload.get("text") or ""))

        if command == "investment-mobile-ai-maintenance":
            return "ai-intel", self.intel_maintenance.run_once()

        # ---------------- strategy lifecycle ----------------
        if command == "investment-mobile-strategy-register":
            definition = StrategyDefinition.from_dict(payload)
            res = self.strategy_registry.register(definition)
            if res.get("ok"):
                self.audit.record("strategy.registered", {
                    "strategy_id": definition.strategy_id,
                    "version": definition.version,
                    "type": definition.strategy_type})
                self._mirror_outbox.append({
                    "operation": "record_strategy",
                    "strategy": res["strategy"]})
            return "strategy", res

        if command == "investment-mobile-strategy-list":
            return "strategy", {
                "ok": True,
                "strategies": self.strategy_registry.list(
                    status=payload.get("status"),
                    market=payload.get("market")),
            }

        if command == "investment-mobile-strategy-transition":
            res = self.strategy_registry.transition(
                str(payload.get("strategy_id") or ""),
                str(payload.get("target") or ""),
                reason=str(payload.get("reason") or ""))
            if res.get("ok"):
                self.audit.record("strategy.transition", {
                    "strategy_id": res["strategy_id"],
                    "status": res["status"],
                    "actor": payload.get("actor")})
            return "strategy", res

        if command == "investment-mobile-strategy-disable":
            return "strategy", self.strategy_registry.disable(
                str(payload.get("strategy_id") or ""),
                reason=str(payload.get("reason") or ""))

        if command == "investment-mobile-strategy-snapshot":
            row = self.strategy_registry.get(
                str(payload.get("strategy_id") or ""),
                payload.get("version"))
            if row is None:
                return "strategy", {"ok": False,
                                    "error_code": "STRATEGY_NOT_FOUND"}
            return "strategy", self.strategy_versions.snapshot(
                StrategyDefinition.from_dict(row),
                code_version=str(payload.get("code_version") or ""),
                data_version=str(payload.get("data_version") or ""),
                model_version=str(payload.get("model_version") or ""),
                cost_assumptions=dict(
                    payload.get("cost_assumptions") or {}))

        if command == "investment-mobile-strategy-versions":
            return "strategy", {
                "ok": True,
                "versions": self.strategy_versions.versions(
                    payload.get("strategy_id")),
            }

        if command == "investment-mobile-strategy-validate":
            res = self.strategy_validation.record_evaluation(
                str(payload.get("strategy_id") or ""),
                int(payload.get("version") or 1),
                str(payload.get("split") or "in_sample"),
                str(payload.get("window") or ""),
                dict(payload.get("parameters") or {}),
                dict(payload.get("metrics") or {}))
            if res.get("ok"):
                self.strategy_versions.attach_validation(
                    str(payload.get("strategy_id") or ""),
                    int(payload.get("version") or 1), res["record"])
            return "strategy", res

        if command == "investment-mobile-strategy-stability":
            return "strategy", self.strategy_validation.stability_check(
                str(payload.get("strategy_id") or ""))

        if command == "investment-mobile-strategy-search-log":
            return "strategy", {
                "ok": True,
                "records": self.strategy_validation.search_log(
                    payload.get("strategy_id")),
            }

        if command == "investment-mobile-strategy-assess":
            return "strategy", self.strategy_evaluator.evaluate(
                dict(payload.get("metrics") or {}),
                objective=str(payload.get("objective") or
                              "long_term_growth"),
                trades=list(payload.get("trades") or []))

        if command == "investment-mobile-strategy-research":
            return "strategy", await self.strategy_research.analyze_result(
                dict(payload.get("run_summary") or {}),
                question=str(payload.get("question") or
                             "分析回測結果與失敗原因"))

        if command == "investment-mobile-strategy-propose":
            return "strategy", await self.strategy_research.propose_draft(
                str(payload.get("strategy_id") or ""),
                dict(payload.get("parameters") or {}),
                reasoning=str(payload.get("reasoning") or ""))

        # ---------------- backtest + audit ----------------
        if command == "investment-mobile-backtest":
            res = self._run_backtest(payload)
            if res.get("ok"):
                self.audit.record("backtest.completed", {
                    "run_id": res["result"]["run_id"],
                    "strategy_id": res["result"]["config"]["strategy_id"],
                    "total_return": res["result"]["total_return"]})
                self._mirror_outbox.append({
                    "operation": "record_backtest_result",
                    "result": res["result"]})
                if payload.get("strategy_id"):
                    self.strategy_versions.attach_backtest(
                        str(payload.get("strategy_id")),
                        int(res["result"]["config"]["strategy_version"]),
                        res["result"]["run_id"])
            return "backtest", res

        if command == "investment-mobile-backtest-results":
            return "backtest", {
                "ok": True,
                "results": self.backtest_engine.results(
                    int(payload.get("limit") or 100)),
            }

        if command == "investment-mobile-backtest-compare":
            return "backtest", self.bt_compare.compare(
                list(payload.get("results") or []))

        if command == "investment-mobile-backtest-submit":
            job_id = str(payload.get("job_id") or
                         f"bt-{int(time.time() * 1000)}")
            return "backtest", self.bt_queue.submit(
                job_id, lambda: self._run_backtest(payload))

        if command == "investment-mobile-backtest-job":
            return "backtest", self.bt_queue.status(
                str(payload.get("job_id") or ""))

        if command == "investment-mobile-backtest-cancel":
            return "backtest", self.bt_queue.cancel(
                str(payload.get("job_id") or ""))

        if command == "investment-mobile-backtest-maintenance":
            return "backtest", self.bt_maintenance.run_once()

        if command == "investment-mobile-fee-rules":
            return "backtest", {"ok": True,
                                "rules": self.bt_cost.rules()}

        if command == "investment-mobile-fee-rule-add":
            from .backtest import FeeRule
            return "backtest", self.bt_cost.add_rule(FeeRule(
                rule_id=str(payload.get("rule_id") or ""),
                broker_id=str(payload.get("broker_id") or ""),
                account_id=str(payload.get("account_id") or ""),
                market=str(payload.get("market") or ""),
                instrument_kind=str(
                    payload.get("instrument_kind") or ""),
                direction=str(payload.get("direction") or ""),
                kind=str(payload.get("kind") or "percent"),
                rate=Decimal(str(payload.get("rate") or 0)),
                minimum=Decimal(str(payload.get("minimum") or 0)),
                currency=str(payload.get("currency") or ""),
                effective_from=str(
                    payload.get("effective_from") or ""),
                effective_to=str(payload.get("effective_to") or ""),
                assumed=bool(payload.get("assumed", True)),
                source_id=str(payload.get("source_id") or "operator")))

        if command == "investment-mobile-universe-register":
            from datetime import date as _date
            return "backtest", self.bt_universe.register_membership(
                str(payload.get("index_or_market") or ""),
                str(payload.get("instrument_id") or ""),
                _date.fromisoformat(str(payload.get("listed_from"))),
                _date.fromisoformat(str(payload["listed_to"]))
                if payload.get("listed_to") else None,
                str(payload.get("source_id") or ""))

        if command == "investment-mobile-universe-members":
            from datetime import date as _date
            return "backtest", {
                "ok": True,
                "members": self.bt_universe.members(
                    str(payload.get("index_or_market") or ""),
                    _date.fromisoformat(
                        str(payload.get("at") or _date.today()))),
            }

        if command == "investment-mobile-broker-capability":
            from .backtest import capability_for
            cap = capability_for(str(payload.get("broker_id") or ""))
            return "backtest", {
                "ok": cap is not None,
                "profile": (
                    {"broker_id": cap.broker_id, "market": cap.market,
                     "order_types": cap.order_types,
                     "extended_hours": cap.extended_hours,
                     "fractional_shares": cap.fractional_shares,
                     "short_selling": cap.short_selling,
                     "notes": cap.notes} if cap else None),
            }

        if command == "investment-mobile-audit-tail":
            return "audit", {
                "ok": True,
                "events": self.audit.tail(payload.get("limit", 50)),
            }

        raise PermissionError("PERMISSION_DENIED")

    # ------------------------------------------------------------------
    def _run_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Route a backtest payload to the market-appropriate engine."""
        row = self.strategy_registry.get(
            str(payload.get("strategy_id") or ""),
            payload.get("strategy_version"))
        if row is None:
            return {"ok": False, "error_code": "STRATEGY_NOT_FOUND"}
        strategy = StrategyDefinition.from_dict(row)
        try:
            from datetime import date as _date
            start = _date.fromisoformat(str(payload.get("start_date")))
            end = _date.fromisoformat(str(payload.get("end_date")))
        except (TypeError, ValueError):
            return {"ok": False, "error_code": "DATE_RANGE_INVALID"}
        cfg = BacktestConfig(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            market=str(payload.get("market") or strategy.market),
            instrument_scope=list(payload.get("instrument_scope")
                                  or strategy.instrument_scope),
            start_date=start, end_date=end,
            initial_capital=Decimal(
                str(payload.get("initial_capital") or 0)),
            currency=str(payload.get("currency") or "TWD"),
            fee_model=str(payload.get("fee_model") or "broker_default"),
            slippage_model=str(payload.get("slippage_model") or "bps"),
            execution_model=str(payload.get("execution_model")
                                or "next_open"),
            data_revision=str(payload.get("data_revision") or "latest"),
            timeframe=str(payload.get("timeframe")
                          or strategy.timeframe),
            assumptions=dict(payload.get("assumptions") or {}))
        if strategy.market == MarketKind.MUTUAL_FUND:
            return self.fund_backtest.run(cfg, strategy)
        if payload.get("legs"):
            return self.portfolio_backtest.replay(
                cfg, list(payload["legs"]))
        return self.backtest_engine.run(cfg, strategy)

    @staticmethod
    def _fund_txn(payload: dict[str, Any], settled_import: bool = False) -> FundTransaction:
        from datetime import date as _date

        def _d(key: str):
            v = payload.get(key)
            return _date.fromisoformat(str(v)) if v else None

        return FundTransaction(
            account_id=str(payload.get("account_id") or ""),
            fund_id=str(payload.get("fund_id") or ""),
            share_class_id=str(payload.get("share_class_id") or "NA"),
            transaction_type=str(
                payload.get("transaction_type") or payload.get("kind") or ""),
            amount=payload.get("amount") or "0",
            units=payload.get("units") or "0",
            confirmed_nav=payload.get("confirmed_nav"),
            currency=str(payload.get("currency") or ""),
            fees=payload.get("fees") or "0",
            application_date=_d("application_date"),
            pricing_date=_d("pricing_date"),
            confirmation_date=_d("confirmation_date"),
            settlement_date=_d("settlement_date"),
            source_id=str(payload.get("source_id") or "manual-import"),
            note=str(payload.get("note") or ""),
        )

    @staticmethod
    def _dt(value: Any) -> Any:
        """Parse ISO-8601 datetime; None stays None (open bound)."""
        if value is None:
            return None
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

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
