"""Trading engine service — governed command surface for the 11 domains.

Commands are own-tool / governance-actor commands on the
``investment-mobile-*`` surface. Business requests keep relaying through
``xingcheng → ai-assistant``; the engines never touch models, networks,
or the business database directly.
"""

from __future__ import annotations

import asyncio
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
            # simulation domain (SHADOW signals + PAPER virtual trading)
            "investment-mobile-sim-status",
            "investment-mobile-sim-tick",
            "investment-mobile-sim-market-event",
            "investment-mobile-sim-recover",
            "investment-mobile-sim-events",
            "investment-mobile-sim-checkpoint",
            "investment-mobile-paper-account-create",
            "investment-mobile-paper-accounts",
            "investment-mobile-paper-cash",
            "investment-mobile-paper-ledger",
            "investment-mobile-paper-deposit",
            "investment-mobile-paper-withdraw",
            "investment-mobile-paper-order-submit",
            "investment-mobile-paper-order-cancel",
            "investment-mobile-paper-order-list",
            "investment-mobile-paper-order-expire",
            "investment-mobile-paper-executions",
            "investment-mobile-paper-positions",
            "investment-mobile-paper-performance",
            "investment-mobile-paper-corporate",
            "investment-mobile-paper-risk-decisions",
            "investment-mobile-shadow-signal",
            "investment-mobile-shadow-signals",
            "investment-mobile-shadow-outcome",
            "investment-mobile-shadow-paper-compare",
            "investment-mobile-sim-strategy-start",
            "investment-mobile-sim-strategy-control",
            "investment-mobile-sim-strategy-runs",
            "investment-mobile-sim-benchmark",
            # live trading core (phase-locked — no real dispatch)
            "investment-mobile-live-status",
            "investment-mobile-live-gate-check",
            "investment-mobile-live-order-submit",
            "investment-mobile-live-order-cancel",
            "investment-mobile-live-order-list",
            "investment-mobile-live-order-resolve",
            "investment-mobile-live-order-resubmit",
            "investment-mobile-live-order-expire",
            "investment-mobile-live-report",
            "investment-mobile-live-auth-issue",
            "investment-mobile-live-auth-revoke",
            "investment-mobile-live-auth-list",
            "investment-mobile-live-risk-evaluate",
            "investment-mobile-live-reconcile",
            "investment-mobile-live-recon-resume",
            "investment-mobile-live-emergency-engage",
            "investment-mobile-live-emergency-release",
            "investment-mobile-live-emergency-status",
            "investment-mobile-live-failure-report",
            "investment-mobile-live-failure-clear",
            "investment-mobile-live-failure-status",
            "investment-mobile-live-broker-capabilities",
            "investment-mobile-live-broker-connect",
            "investment-mobile-live-credential-register",
            "investment-mobile-live-credential-status",
            "investment-mobile-live-snapshot-ingest",
            "investment-mobile-live-audit-tail",
            "investment-mobile-live-recover",
            "investment-mobile-live-breaker-resume",
            "investment-mobile-live-exposure-register",
            # offline broker integration foundation (no real connection)
            "investment-mobile-broker-state-list",
            "investment-mobile-broker-state-set",
            "investment-mobile-broker-profile",
            "investment-mobile-offline-gate-status",
            "investment-mobile-offline-account-create",
            "investment-mobile-offline-account-list",
            "investment-mobile-offline-account-view",
            "investment-mobile-offline-cash-set",
            "investment-mobile-offline-holding-set",
            "investment-mobile-offline-holding-remove",
            "investment-mobile-offline-transaction-add",
            "investment-mobile-offline-dividend-add",
            "investment-mobile-import-start",
            "investment-mobile-import-commit",
            "investment-mobile-import-rollback",
            "investment-mobile-import-batches",
            "investment-mobile-portfolio-unified",
            "investment-mobile-broker-sim-list",
            "investment-mobile-broker-sim-order",
            "investment-mobile-broker-sim-fill",
            "investment-mobile-broker-sim-cancel",
            # unified asset-management center (offline authority)
            "investment-mobile-asset-account-list",
            "investment-mobile-asset-account-register",
            "investment-mobile-asset-account-rename",
            "investment-mobile-asset-position-list",
            "investment-mobile-asset-position-reserve",
            "investment-mobile-asset-position-release",
            "investment-mobile-asset-transaction-record",
            "investment-mobile-asset-transaction-correct",
            "investment-mobile-asset-transaction-list",
            "investment-mobile-asset-cash-deposit",
            "investment-mobile-asset-cash-withdraw",
            "investment-mobile-asset-cash-adjust",
            "investment-mobile-asset-cash-view",
            "investment-mobile-asset-cash-reserve",
            "investment-mobile-asset-cash-release",
            "investment-mobile-asset-cost-view",
            "investment-mobile-asset-cost-method",
            "investment-mobile-asset-currency-convert",
            "investment-mobile-asset-currency-staleness",
            "investment-mobile-asset-value",
            "investment-mobile-asset-performance",
            "investment-mobile-asset-period-pnl",
            "investment-mobile-asset-income-record",
            "investment-mobile-asset-income-list",
            "investment-mobile-asset-income-cashflow",
            "investment-mobile-asset-income-breakdown",
            "investment-mobile-asset-allocation-analyze",
            "investment-mobile-asset-allocation-target",
            "investment-mobile-asset-allocation-propose",
            "investment-mobile-asset-exposure-basket",
            "investment-mobile-asset-exposure-related",
            "investment-mobile-asset-exposure-analyze",
            "investment-mobile-asset-snapshot-capture",
            "investment-mobile-asset-snapshot-history",
            "investment-mobile-asset-risk-report",
            "investment-mobile-asset-ai-analyze",
            "investment-mobile-asset-recommend",
            "investment-mobile-asset-maintenance-run",
            "investment-mobile-asset-maintenance-status",
            "investment-mobile-asset-environment-view",
            "investment-mobile-import-diff",
            # 星澄 monitoring center — advisory/alert layer, never trades
            "investment-mobile-monitor-overview",
            "investment-mobile-monitor-scan-tw",
            "investment-mobile-monitor-scan-us",
            "investment-mobile-monitor-scan-fund",
            "investment-mobile-monitor-session-us",
            "investment-mobile-monitor-events",
            "investment-mobile-monitor-event-resolve",
            "investment-mobile-monitor-data-gate",
            "investment-mobile-monitor-alert-rule-set",
            "investment-mobile-monitor-alert-rules",
            "investment-mobile-monitor-alert-evaluate",
            "investment-mobile-monitor-notifications",
            "investment-mobile-monitor-notification-read",
            "investment-mobile-monitor-recommend",
            "investment-mobile-monitor-rec-list",
            "investment-mobile-monitor-rec-transition",
            "investment-mobile-monitor-rec-outcome",
            "investment-mobile-monitor-rec-expire",
            "investment-mobile-monitor-scan-opportunities",
            "investment-mobile-monitor-risk-check",
            "investment-mobile-monitor-cross-market",
            "investment-mobile-monitor-reallocation",
            "investment-mobile-monitor-reallocation-list",
            "investment-mobile-monitor-report",
            "investment-mobile-monitor-report-list",
            "investment-mobile-monitor-schedule-due",
            "investment-mobile-monitor-schedule-ran",
            "investment-mobile-monitor-orchestrate",
            "investment-mobile-monitor-orchestrator-stats",
            "investment-mobile-monitor-maintenance-run",
            "investment-mobile-monitor-maintenance-status",
            # autonomous simulated trading — SHADOW/PAPER only
            "investment-mobile-autotrade-overview",
            "investment-mobile-autotrade-strategy-register",
            "investment-mobile-autotrade-strategy-list",
            "investment-mobile-autotrade-strategy-transition",
            "investment-mobile-autotrade-strategy-config",
            "investment-mobile-autotrade-event",
            "investment-mobile-autotrade-cycle",
            "investment-mobile-autotrade-capital-set",
            "investment-mobile-autotrade-capital-get",
            "investment-mobile-autotrade-capital-check",
            "investment-mobile-autotrade-resources",
            "investment-mobile-autotrade-job-schedule",
            "investment-mobile-autotrade-job-control",
            "investment-mobile-autotrade-job-due",
            "investment-mobile-autotrade-session",
            "investment-mobile-autotrade-risk-check",
            "investment-mobile-autotrade-halt",
            "investment-mobile-autotrade-recover",
            "investment-mobile-autotrade-performance",
            "investment-mobile-autotrade-performance-curve",
            "investment-mobile-autotrade-stability",
            "investment-mobile-autotrade-research",
            "investment-mobile-autotrade-experiment-start",
            "investment-mobile-autotrade-experiment-advance",
            "investment-mobile-autotrade-experiment-list",
            "investment-mobile-autotrade-overfit-record",
            "investment-mobile-autotrade-overfit-history",
            "investment-mobile-autotrade-fund-analyze",
            "investment-mobile-autotrade-report",
            "investment-mobile-autotrade-report-list",
            "investment-mobile-autotrade-maintenance-run",
            "investment-mobile-autotrade-maintenance-status",
            "investment-mobile-autotrade-workload-stats",
            # phase-13 runtime layer — bounded resources, maintenance,
            # recovery, power-state, lifecycle, health, retention
            "investment-mobile-perf-overview",
            "investment-mobile-perf-health",
            "investment-mobile-perf-metrics",
            "investment-mobile-perf-budget-status",
            "investment-mobile-perf-budget-set",
            "investment-mobile-perf-cache-stats",
            "investment-mobile-perf-cache-invalidate",
            "investment-mobile-perf-subscribe",
            "investment-mobile-perf-unsubscribe",
            "investment-mobile-perf-sub-status",
            "investment-mobile-perf-indicator-update",
            "investment-mobile-perf-job-submit",
            "investment-mobile-perf-job-control",
            "investment-mobile-perf-job-status",
            "investment-mobile-perf-inference-submit",
            "investment-mobile-perf-inference-status",
            "investment-mobile-perf-maintenance-run",
            "investment-mobile-perf-maintenance-status",
            "investment-mobile-perf-recovery-run",
            "investment-mobile-perf-recovery-history",
            "investment-mobile-perf-power-event",
            "investment-mobile-perf-lifecycle-status",
            "investment-mobile-perf-lifecycle-transition",
            "investment-mobile-perf-retention-run",
            "investment-mobile-perf-retention-status",
            "investment-mobile-perf-pool-stats",
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
        # simulation layer — dedicated paper ledgers, no broker access;
        # OMS PAPER branch delegates to it so formal state is untouched
        from .simulation import SimulationTradingEngine
        self.sim = SimulationTradingEngine(
            state_dir, self.mode_gate, self.candle_store,
            self.calendar, self.fund_engine, self.bt_cost)
        self.oms.attach_simulation(self.sim)
        # formal trading core — LIVE stays phase-locked this phase:
        # dispatch_enabled=False, adapters UNKNOWN, gate PHASE_LOCKED
        from .live import LiveTradingCore
        self.live = LiveTradingCore(
            state_dir, mode_gate=self.mode_gate,
            accounts=self.accounts, risk_engine=self.risk,
            dispatch_enabled=False)
        # offline broker-integration foundation — no transport, ever
        from .broker.offline_gate import BrokerOfflineGate
        from .broker.state import BrokerConnectionTracker
        from .offline import (InvestmentImportService,
                              OfflineAccountService,
                              UnifiedInvestmentPortfolio)
        from .broker.mock import (MockCathayTwAdapter,
                                  MockFubonSubBrokerageAdapter)
        self.offline_gate = BrokerOfflineGate(state_dir)
        self.broker_connections = BrokerConnectionTracker(state_dir)
        self.offline_accounts = OfflineAccountService(state_dir)
        self.importer = InvestmentImportService(
            state_dir, self.offline_accounts)
        self.unified_portfolio = UnifiedInvestmentPortfolio(
            self.offline_accounts, self.fx)
        self.broker_sims = {
            a.broker_id: a for a in (
                MockCathayTwAdapter(state_dir),
                MockFubonSubBrokerageAdapter(state_dir))
        }
        # unified asset-management center — orchestration layer over the
        # offline journal (no second authority), currency via the
        # existing FX service; advisory AI surface is read-only
        from .assets import UnifiedPortfolioEngine
        self.assets = UnifiedPortfolioEngine(
            state_dir, self.offline_accounts, self.fx,
            candle_store=self.candle_store, sim=self.sim,
            live=self.live, cost_estimator=self.bt_cost)
        # 星澄 monitoring center — deterministic monitors + alerts +
        # advisory recommendations over the existing engines; emits
        # events/notifications/reports, never issues orders
        from .monitoring import InvestmentMonitoringEngine
        self.monitoring = InvestmentMonitoringEngine(
            state_dir, candle_store=self.candle_store,
            calendar=self.calendar, market_engine=self.market_engine,
            fund_engine=self.fund_engine, fx=self.fx,
            assets=self.assets, intel=self.intel,
            instruments=self.instruments,
            cost_estimator=self.bt_cost)
        # Autonomous simulated trading — orchestrates strategies over
        # the sim stack; SHADOW records signals, PAPER fills through the
        # mock broker contract. No path reaches a real broker.
        from .autotrade import AutoTradingEngine
        self.autotrade = AutoTradingEngine(
            state_dir, sim=self.sim, calendar=self.calendar,
            monitoring=self.monitoring, intel=self.intel,
            strategy_registry=self.strategy_registry,
            fund_engine=self.fund_engine)
        # phase-13 runtime layer — bounded resources, shared market
        # subscriptions, incremental indicators, job/maintenance/
        # recovery/power/lifecycle/health/retention. Observes and bounds
        # the engines; holds no account/trade authority itself.
        from .perf import PerformanceRuntime
        self.perf = PerformanceRuntime(autotrade=self.autotrade)

    def close(self) -> None:
        """Release held journal/db handles (Windows file locks)."""
        for svc in (self.sim, self.audit, self.candle_store,
                    self.live, self.offline_accounts, self.assets,
                    self.monitoring, self.autotrade):
            try:
                svc.close()
            except Exception:
                pass

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
            target = str(payload.get("mode") or "").upper()
            if target == "LIVE" and not self.live.gate.dispatch_allowed():
                result = {
                    "ok": False, "error_code": "LIVE_PHASE_LOCKED",
                    "mode": self.mode_gate.mode.value,
                    "readiness": self.live.gate.readiness()}
            else:
                result = self.mode_gate.set_mode(target)
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

        # ---------------- simulation (SHADOW + PAPER) ----------------
        if command == "investment-mobile-sim-status":
            return "sim", self.sim.status()

        if command == "investment-mobile-sim-tick":
            return "sim", self.sim.tick(dict(payload))

        if command == "investment-mobile-sim-market-event":
            return "sim", self.sim.process_market_event(
                str(payload.get("instrument_id") or ""),
                str(payload.get("market") or ""))

        if command == "investment-mobile-sim-recover":
            return "sim", self.sim.recover()

        if command == "investment-mobile-sim-events":
            return "sim", {
                "ok": True,
                "events": self.sim.recovery.events(
                    int(payload.get("limit") or 500))}

        if command == "investment-mobile-sim-checkpoint":
            return "sim", self.sim.recovery.checkpoint(
                dict(payload.get("state") or {}))

        if command == "investment-mobile-paper-account-create":
            res = self.sim.create_account(payload)
            if res.get("ok"):
                self.audit.record("sim.paper_account", {
                    "account_id": res["account"]["account_id"]})
            return "sim", res

        if command == "investment-mobile-paper-accounts":
            return "sim", {"ok": True,
                           "accounts": self.sim.accounts.list()}

        if command == "investment-mobile-paper-cash":
            return "sim", self.sim.accounts.cash(
                str(payload.get("account_id") or ""))

        if command == "investment-mobile-paper-ledger":
            return "sim", {
                "ok": True,
                "entries": self.sim.accounts.ledger(
                    payload.get("account_id"),
                    int(payload.get("limit") or 500))}

        if command == "investment-mobile-paper-deposit":
            return "sim", self.sim.deposit(
                str(payload.get("account_id") or ""),
                payload.get("amount") or "0")

        if command == "investment-mobile-paper-withdraw":
            return "sim", self.sim.withdraw(
                str(payload.get("account_id") or ""),
                payload.get("amount") or "0")

        if command == "investment-mobile-paper-order-submit":
            res = self.sim.submit_order(payload)
            self.audit.record("sim.paper_order", {
                "order_id": (res.get("order") or {}).get("order_id"),
                "ok": res.get("ok"),
                "mode": self.mode_gate.mode.value})
            if res.get("execution"):
                self._mirror_outbox.append({
                    "operation": "record_paper_execution",
                    "execution": res["execution"]})
            return "sim", res

        if command == "investment-mobile-paper-order-cancel":
            return "sim", self.sim.cancel_order(
                str(payload.get("order_id") or ""))

        if command == "investment-mobile-paper-order-list":
            return "sim", {
                "ok": True,
                "orders": self.sim.orders.list(
                    payload.get("account_id"), payload.get("status"))}

        if command == "investment-mobile-paper-order-expire":
            return "sim", self.sim.expire_due()

        if command == "investment-mobile-paper-executions":
            return "sim", {
                "ok": True,
                "executions": self.sim.orders.executions(
                    payload.get("order_id"))}

        if command == "investment-mobile-paper-positions":
            return "sim", {
                "ok": True,
                "positions": self.sim.positions.list(
                    payload.get("account_id"))}

        if command == "investment-mobile-paper-performance":
            marks = {str(k): Decimal(str(v)) for k, v in
                     dict(payload.get("marks") or {}).items()}
            return "sim", self.sim.performance.report(
                str(payload.get("account_id") or ""), marks)

        if command == "investment-mobile-paper-corporate":
            return "sim", self.sim.apply_corporate(
                str(payload.get("account_id") or ""),
                str(payload.get("instrument_id") or ""),
                str(payload.get("kind") or ""),
                ratio=payload.get("ratio") or "1",
                cash_amount=payload.get("cash_amount") or "0")

        if command == "investment-mobile-paper-risk-decisions":
            return "sim", {
                "ok": True,
                "decisions": self.sim.risk.decisions(
                    int(payload.get("limit") or 100))}

        if command == "investment-mobile-shadow-signal":
            res = self.sim.record_shadow_signal(payload)
            if res.get("ok"):
                self.audit.record("sim.shadow_signal", {
                    "signal_id": res["signal"]["signal_id"],
                    "side": res["signal"]["side"]})
                self._mirror_outbox.append({
                    "operation": "record_shadow_signal",
                    "signal": res["signal"]})
            return "sim", res

        if command == "investment-mobile-shadow-signals":
            return "sim", {
                "ok": True,
                "signals": self.sim.shadow.signals(
                    payload.get("instrument_id"),
                    int(payload.get("limit") or 200))}

        if command == "investment-mobile-shadow-outcome":
            return "sim", self.sim.signal_outcome(
                str(payload.get("signal_id") or ""))

        if command == "investment-mobile-shadow-paper-compare":
            sig = next((s for s in self.sim.shadow.signals()
                        if s["signal_id"] ==
                        str(payload.get("signal_id") or "")), None)
            if sig is None:
                return "sim", {"ok": False,
                               "error_code": "SIGNAL_NOT_FOUND"}
            return "sim", self.sim.shadow_paper.compare(
                sig,
                self.sim.orders.executions(),
                Decimal(str(payload.get("market_end_price") or
                            sig.get("reference_price") or "1")))

        if command == "investment-mobile-sim-strategy-start":
            from .simulation import StrategyRun
            row = self.strategy_registry.get(
                str(payload.get("strategy_id") or ""),
                payload.get("strategy_version"))
            if row is None:
                return "sim", {"ok": False,
                               "error_code": "STRATEGY_NOT_FOUND"}
            run = StrategyRun(
                strategy_id=row["strategy_id"],
                strategy_version=int(row["version"]),
                paper_account_id=str(payload.get("paper_account_id")
                                     or ""),
                allocated_capital=Decimal(
                    str(payload.get("allocated_capital") or 0)),
                risk_budget=Decimal(str(payload.get("risk_budget")
                                        or 0)))
            res = self.sim.start_loop(run, row)
            if res.get("ok"):
                self.audit.record("sim.strategy_start", {
                    "run_id": run.run_id,
                    "strategy_id": run.strategy_id})
            return "sim", res

        if command == "investment-mobile-sim-strategy-control":
            return "sim", self.sim.loop_control(
                str(payload.get("run_id") or ""),
                str(payload.get("action") or ""))

        if command == "investment-mobile-sim-strategy-runs":
            return "sim", {
                "ok": True,
                "runs": self.sim.coordinator.runs(
                    payload.get("status"))}

        if command == "investment-mobile-sim-benchmark":
            return "sim", self.sim.benchmark.compare(
                list(payload.get("equity_curve") or []),
                str(payload.get("benchmark_id") or ""))

        # ---------------- live trading core (phase-locked) ----------------
        if command == "investment-mobile-live-status":
            return "live", {"ok": True, **self.live.status()}

        if command == "investment-mobile-live-gate-check":
            return "live", {"ok": True, **self.live.gate.readiness()}

        if command == "investment-mobile-live-order-submit":
            return "live", self.live.submit(payload)

        if command == "investment-mobile-live-order-cancel":
            return "live", self.live.orders.cancel(
                str(payload.get("order_id") or ""),
                by=str(payload.get("by") or "operator"))

        if command == "investment-mobile-live-order-list":
            orders = self.live.orders.list()
            if payload.get("open_only"):
                orders = [o for o in orders
                          if o["state"] not in
                          ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")]
            return "live", {"ok": True, "orders": orders}

        if command == "investment-mobile-live-order-resolve":
            return "live", self.live.orders.resolve_unknown(
                str(payload.get("order_id") or ""))

        if command == "investment-mobile-live-order-resubmit":
            return "live", self.live.orders.resubmit(
                str(payload.get("order_id") or ""))

        if command == "investment-mobile-live-order-expire":
            return "live", self.live.orders.expire(
                str(payload.get("order_id") or ""))

        if command == "investment-mobile-live-report":
            return "live", self.live.orders.record_report(payload)

        if command == "investment-mobile-live-auth-issue":
            return "live", self.live.authorization.issue(payload)

        if command == "investment-mobile-live-auth-revoke":
            return "live", self.live.authorization.revoke(
                str(payload.get("authorization_id") or ""),
                by=str(payload.get("by") or ""))

        if command == "investment-mobile-live-auth-list":
            return "live", {"ok": True, "authorizations":
                            self.live.authorization.list(
                                payload.get("account_id"))}

        if command == "investment-mobile-live-risk-evaluate":
            return "live", self.live.risk.evaluate(payload).to_dict()

        if command == "investment-mobile-live-reconcile":
            return "live", self.live.reconciliation.reconcile(
                str(payload.get("account_id") or ""),
                local_orders=list(payload.get("local_orders") or []),
                local_executions=list(
                    payload.get("local_executions") or []),
                local_positions=list(
                    payload.get("local_positions") or []),
                local_cash=dict(payload.get("local_cash") or {}),
                broker_snapshot=payload.get("broker_snapshot"))

        if command == "investment-mobile-live-recon-resume":
            return "live", self.live.reconciliation.resume(
                str(payload.get("account_id") or ""),
                by=str(payload.get("by") or ""),
                evidence=str(payload.get("evidence") or ""))

        if command == "investment-mobile-live-emergency-engage":
            return "live", self.live.emergency.engage(
                str(payload.get("scope") or "ALL"),
                scope_key=str(payload.get("scope_key") or ""),
                by=str(payload.get("by") or "operator"),
                reason=str(payload.get("reason") or ""))

        if command == "investment-mobile-live-emergency-release":
            return "live", self.live.emergency.release(
                str(payload.get("scope") or "ALL"),
                scope_key=str(payload.get("scope_key") or ""),
                by=str(payload.get("by") or ""),
                reason=str(payload.get("reason") or ""))

        if command == "investment-mobile-live-emergency-status":
            return "live", {"ok": True, **self.live.emergency.status()}

        if command == "investment-mobile-live-failure-report":
            return "live", self.live.failure.report(
                str(payload.get("kind") or ""),
                detail=str(payload.get("detail") or ""))

        if command == "investment-mobile-live-failure-clear":
            return "live", self.live.failure.clear(
                str(payload.get("kind") or ""),
                reconciled=bool(payload.get("reconciled")))

        if command == "investment-mobile-live-failure-status":
            return "live", {"ok": True, **self.live.failure.status()}

        if command == "investment-mobile-live-broker-capabilities":
            return "live", {"ok": True, "capabilities":
                            self.live.gateway.capabilities(
                                str(payload.get("broker_id") or ""))}

        if command == "investment-mobile-live-broker-connect":
            return "live", self.live.gateway.connect(
                str(payload.get("broker_id") or ""))

        if command == "investment-mobile-live-credential-register":
            return "live", self.live.credentials.register_reference(
                str(payload.get("account_id") or ""),
                str(payload.get("credman_suffix") or ""))

        if command == "investment-mobile-live-credential-status":
            return "live", self.live.credentials.status(
                str(payload.get("account_id") or ""))

        if command == "investment-mobile-live-snapshot-ingest":
            return "live", self.live.live_accounts.ingest_broker_snapshot(
                str(payload.get("account_id") or ""),
                dict(payload.get("snapshot") or {}))

        if command == "investment-mobile-live-audit-tail":
            return "live", {"ok": True, "events":
                            self.live.persistence.tail(
                                "live_audit",
                                int(payload.get("limit") or 50))}

        if command == "investment-mobile-live-recover":
            return "live", self.live.recover()

        if command == "investment-mobile-live-breaker-resume":
            return "live", self.live.risk.loss.resume(
                str(payload.get("scope") or ""),
                by=str(payload.get("by") or ""),
                evidence=str(payload.get("evidence") or ""))

        if command == "investment-mobile-live-exposure-register":
            self.live.risk.position.register_related(
                str(payload.get("group_id") or ""),
                list(payload.get("instrument_ids") or []))
            if payload.get("sector") and payload.get("instrument_id"):
                self.live.risk.position.register_sector(
                    str(payload["instrument_id"]),
                    str(payload["sector"]))
            return "live", {"ok": True}

        # --------- offline broker integration foundation --------------
        if command == "investment-mobile-offline-gate-status":
            return "offline", self.offline_gate.status()

        if command == "investment-mobile-broker-state-list":
            out = []
            for b in self.brokers.list_brokers():
                conn = self.broker_connections.get(b["broker_id"])
                out.append({**b, "connection": conn,
                            "capabilities": self.brokers.adapter_for(
                                b["broker_id"]).capabilities()})
            return "offline", {"ok": True, "brokers": out}

        if command == "investment-mobile-broker-state-set":
            # Only offline-reachable states accepted; CONNECTED etc.
            # denied inside the tracker — and mutation attempts by AI
            # land on the gate's audit trail.
            actor = str(payload.get("actor") or "")
            if actor.lower() in ("ai", "xingcheng", "model", "assistant"):
                return "offline", self.offline_gate.attempt_mutation(
                    actor, "broker_connection_state",
                    payload.get("state"))
            r = self.broker_connections.set(
                str(payload.get("broker_id") or ""),
                str(payload.get("state") or ""),
                actor=actor or "operator",
                reason=str(payload.get("reason") or ""))
            if r.get("ok"):
                self._mirror_outbox.append({
                    "operation": "record_broker_status",
                    "brokers": [self.broker_connections.get(
                        b["broker_id"]) | {"capabilities":
                            self.brokers.adapter_for(
                                b["broker_id"]).capabilities()}
                        for b in self.brokers.list_brokers()],
                    "offline_gate": self.offline_gate.status()})
            return "offline", r

        if command == "investment-mobile-broker-profile":
            adapter = self.brokers.adapter_for(
                str(payload.get("broker_id") or ""))
            if adapter is None:
                return "offline", {"ok": False,
                                   "error_code": "BROKER_UNKNOWN"}
            return "offline", {
                "ok": True, "broker_id": adapter.broker_id,
                "market": adapter.market, "label": adapter.label,
                "capabilities": adapter.capabilities(),
                "market_traits": getattr(
                    adapter, "declared_market_traits", {}),
                "fee_model": getattr(adapter, "fee_model", {}),
                "error_model": getattr(adapter, "error_model", {}),
                "session_model": getattr(adapter, "session_model", {}),
                "connection": self.broker_connections.get(
                    adapter.broker_id),
            }

        if command == "investment-mobile-offline-account-create":
            r = self.offline_accounts.create_account(
                str(payload.get("kind") or ""),
                str(payload.get("label") or ""),
                source=str(payload.get("source") or "MANUAL").upper(),
                account_id=payload.get("account_id"))
            if r.get("ok"):
                self._mirror_outbox.append({
                    "operation": "record_offline_accounts",
                    "accounts": self.offline_accounts.list_accounts()})
            return "offline", r

        if command == "investment-mobile-offline-account-list":
            return "offline", {"ok": True,
                "accounts": self.offline_accounts.list_accounts()}

        if command == "investment-mobile-offline-account-view":
            return "offline", self.offline_accounts.account_view(
                str(payload.get("account_id") or ""))

        if command == "investment-mobile-offline-cash-set":
            return "offline", self.offline_accounts.set_cash(
                str(payload.get("account_id") or ""),
                str(payload.get("currency") or ""),
                payload.get("amount"),
                source=str(payload.get("source") or "MANUAL").upper())

        if command == "investment-mobile-offline-holding-set":
            return "offline", self.offline_accounts.set_holding(
                str(payload.get("account_id") or ""),
                str(payload.get("instrument_id") or ""),
                payload.get("quantity"), payload.get("avg_cost", "0"),
                source=str(payload.get("source") or "MANUAL").upper())

        if command == "investment-mobile-offline-holding-remove":
            return "offline", self.offline_accounts.remove_holding(
                str(payload.get("account_id") or ""),
                str(payload.get("instrument_id") or ""),
                source=str(payload.get("source") or "MANUAL").upper())

        if command == "investment-mobile-offline-transaction-add":
            return "offline", self.offline_accounts.add_transaction(
                str(payload.get("account_id") or ""), payload,
                source=str(payload.get("source") or "MANUAL").upper())

        if command == "investment-mobile-offline-dividend-add":
            return "offline", self.offline_accounts.add_dividend(
                str(payload.get("account_id") or ""), payload,
                source=str(payload.get("source") or "MANUAL").upper())

        if command == "investment-mobile-import-start":
            return "offline", self.importer.start(
                str(payload.get("file") or ""),
                target=str(payload.get("target") or ""),
                account_id=str(payload.get("account_id") or ""))

        if command == "investment-mobile-import-commit":
            r = self.importer.commit(
                str(payload.get("batch_id") or ""),
                mapping=dict(payload.get("mapping") or {}),
                confirm=bool(payload.get("confirm")))
            if r.get("ok") and r.get("status") == "COMMITTED":
                self._mirror_outbox.append({
                    "operation": "record_import_batches",
                    "batches": self.importer.batches()})
                self._mirror_outbox.append({
                    "operation": "record_offline_accounts",
                    "accounts": self.offline_accounts.list_accounts()})
            return "offline", r

        if command == "investment-mobile-import-rollback":
            return "offline", self.importer.rollback(
                str(payload.get("batch_id") or ""))

        if command == "investment-mobile-import-batches":
            return "offline", {"ok": True,
                "batches": self.importer.batches(
                    payload.get("account_id"))}

        if command == "investment-mobile-portfolio-unified":
            r = self.unified_portfolio.summary(
                display_currency=str(
                    payload.get("display_currency") or "TWD"),
                prices=dict(payload.get("prices") or {}))
            self._mirror_outbox.append({
                "operation": "record_unified_portfolio",
                "portfolio": r})
            return "offline", r

        if command == "investment-mobile-broker-sim-list":
            return "offline", {"ok": True, "simulated": True,
                "environments": [
                    {"broker_id": a.broker_id, "market": a.market,
                     "label": a.label, "simulated": True,
                     "fee_model": a.fee_model,
                     "capabilities": a.capabilities()}
                    for a in self.broker_sims.values()]}

        if command == "investment-mobile-broker-sim-order":
            sim = self.broker_sims.get(str(payload.get("broker_id") or ""))
            if sim is None:
                return "offline", {"ok": False,
                                   "error_code": "SIM_BROKER_UNKNOWN"}
            r = sim.place_order(**{
                k: v for k, v in payload.items() if k != "broker_id"})
            self._mirror_outbox.append({
                "operation": "record_broker_sim_event",
                "event": {"event_type": "order", "payload": r,
                          "simulated": True}})
            return "offline", r

        if command == "investment-mobile-broker-sim-fill":
            sim = self.broker_sims.get(str(payload.get("broker_id") or ""))
            if sim is None:
                return "offline", {"ok": False,
                                   "error_code": "SIM_BROKER_UNKNOWN"}
            r = sim.fill(
                str(payload.get("broker_order_id") or ""),
                payload.get("quantity"), payload.get("price"),
                partial=bool(payload.get("partial")))
            self._mirror_outbox.append({
                "operation": "record_broker_sim_event",
                "event": {"event_type": "fill", "payload": r,
                          "simulated": True}})
            return "offline", r

        if command == "investment-mobile-broker-sim-cancel":
            sim = self.broker_sims.get(str(payload.get("broker_id") or ""))
            if sim is None:
                return "offline", {"ok": False,
                                   "error_code": "SIM_BROKER_UNKNOWN"}
            r = sim.cancel_order(
                broker_order_id=payload.get("broker_order_id"))
            self._mirror_outbox.append({
                "operation": "record_broker_sim_event",
                "event": {"event_type": "cancel", "payload": r,
                          "simulated": True}})
            return "offline", r

        # --------- unified asset-management center --------------------
        # Read/write boundary: every mutating command rejects an AI
        # actor up-front; AI only reaches the intelligence/recommend
        # views which never mutate authoritative records.
        if command.startswith("investment-mobile-asset-") or \
                command == "investment-mobile-import-diff":
            actor = str(payload.get("actor") or "")
            is_ai = actor.lower() in (
                "ai", "xingcheng", "model", "assistant")
            mutating = command in {
                "investment-mobile-asset-account-register",
                "investment-mobile-asset-account-rename",
                "investment-mobile-asset-position-reserve",
                "investment-mobile-asset-position-release",
                "investment-mobile-asset-transaction-record",
                "investment-mobile-asset-transaction-correct",
                "investment-mobile-asset-cash-deposit",
                "investment-mobile-asset-cash-withdraw",
                "investment-mobile-asset-cash-adjust",
                "investment-mobile-asset-cash-reserve",
                "investment-mobile-asset-cash-release",
                "investment-mobile-asset-cost-method",
                "investment-mobile-asset-income-record",
                "investment-mobile-asset-allocation-target",
                "investment-mobile-asset-exposure-basket",
                "investment-mobile-asset-exposure-related",
            }
            if is_ai and mutating:
                return "assets", {"ok": False,
                                  "error_code": "AI_MUTATION_DENIED"}
            r = self._handle_asset(command, payload)
            if r.get("ok") and command in (
                    "investment-mobile-asset-value",
                    "investment-mobile-asset-allocation-analyze",
                    "investment-mobile-asset-exposure-analyze"):
                self._mirror_outbox.append({
                    "operation": "record_asset_analysis",
                    "command": command, "result": r})
            return "assets", r

        # --------- 星澄 monitoring center ------------------------------
        if command.startswith("investment-mobile-monitor-"):
            r = self._handle_monitoring(command, payload)
            if asyncio.iscoroutine(r):
                r = await r
            if r.get("ok") and command in (
                    "investment-mobile-monitor-overview",
                    "investment-mobile-monitor-scan-tw",
                    "investment-mobile-monitor-scan-us",
                    "investment-mobile-monitor-scan-fund",
                    "investment-mobile-monitor-events",
                    "investment-mobile-monitor-rec-list",
                    "investment-mobile-monitor-notifications",
                    "investment-mobile-monitor-risk-check",
                    "investment-mobile-monitor-cross-market",
                    "investment-mobile-monitor-scan-opportunities",
                    "investment-mobile-monitor-reallocation-list",
                    "investment-mobile-monitor-report",
                    "investment-mobile-monitor-report-list"):
                self._mirror_outbox.append({
                    "operation": "record_monitoring",
                    "command": command, "result": r})
            return "monitoring", r

        # --------- autonomous simulated trading -----------------------
        if command.startswith("investment-mobile-autotrade-"):
            r = self._handle_autotrade(command, payload)
            if asyncio.iscoroutine(r):
                r = await r
            if r.get("ok") and command in (
                    "investment-mobile-autotrade-overview",
                    "investment-mobile-autotrade-strategy-list",
                    "investment-mobile-autotrade-performance",
                    "investment-mobile-autotrade-report-list",
                    "investment-mobile-autotrade-risk-check"):
                self._mirror_outbox.append({
                    "operation": "record_autotrade",
                    "command": command, "result": r})
            return "autotrade", r

        # --------- phase-13 runtime layer ------------------------------
        if command.startswith("investment-mobile-perf-"):
            r = self._handle_perf(command, payload)
            if asyncio.iscoroutine(r):
                r = await r
            return "perf", r

        raise PermissionError("PERMISSION_DENIED")

    # ------------------------------------------------------------------
    def _handle_autotrade(self, command: str,
                          payload: dict[str, Any]) -> dict[str, Any]:
        """Autonomous simulated-trading commands. AI actors may read and
        research but can never register/transition/fund/halt/recover a
        strategy or advance experiments."""
        a = self.autotrade
        actor = str(payload.get("actor") or "")
        is_ai = actor.lower() in ("ai", "xingcheng", "model",
                                  "assistant")
        mutating = command in {
            "investment-mobile-autotrade-strategy-register",
            "investment-mobile-autotrade-strategy-transition",
            "investment-mobile-autotrade-strategy-config",
            "investment-mobile-autotrade-capital-set",
            "investment-mobile-autotrade-halt",
            "investment-mobile-autotrade-recover",
            "investment-mobile-autotrade-job-schedule",
            "investment-mobile-autotrade-job-control",
            "investment-mobile-autotrade-experiment-start",
            "investment-mobile-autotrade-experiment-advance",
        }
        if is_ai and mutating:
            return {"ok": False, "error_code": "AI_MUTATION_DENIED"}

        if command == "investment-mobile-autotrade-overview":
            return a.overview()

        if command == "investment-mobile-autotrade-strategy-register":
            return a.manager.register(
                strategy_id=str(payload.get("strategy_id") or ""),
                strategy_version=int(
                    payload.get("strategy_version") or 1),
                market=str(payload.get("market") or ""),
                instrument_scope=list(
                    payload.get("instrument_scope") or []),
                strategy_type=str(
                    payload.get("strategy_type") or "MOMENTUM"),
                parameters=dict(payload.get("parameters") or {}),
                execution_mode=str(
                    payload.get("execution_mode") or "SHADOW"),
                account_id=str(payload.get("account_id") or ""),
                auto_recover=bool(payload.get("auto_recover")),
                session_policy=dict(
                    payload.get("session_policy") or {}),
                ai_policy=str(
                    payload.get("ai_policy") or "DETERMINISTIC"))
        if command == "investment-mobile-autotrade-strategy-list":
            return {"ok": True,
                    "strategies": a.manager.list(
                        payload.get("state"))}
        if command == "investment-mobile-autotrade-strategy-transition":
            return a.manager.transition(
                str(payload.get("run_id") or ""),
                str(payload.get("target") or ""),
                actor=actor or "user",
                reason=str(payload.get("reason") or ""))
        if command == "investment-mobile-autotrade-strategy-config":
            return a.manager.update_config(
                str(payload.get("run_id") or ""),
                dict(payload.get("patch") or {}),
                actor=actor or "user")

        if command == "investment-mobile-autotrade-event":
            return a.on_market_event(
                str(payload.get("event_type") or "QUOTE_UPDATED"),
                market=str(payload.get("market") or ""),
                instrument_id=str(payload.get("instrument_id") or ""),
                source_id=str(payload.get("source_id") or "test"),
                data_revision=str(payload.get("data_revision") or ""),
                timestamp=payload.get("timestamp"))
        if command == "investment-mobile-autotrade-cycle":
            return a.coordinator.run_cycle(
                str(payload.get("run_id") or ""),
                dict(payload.get("event") or {}))

        if command == "investment-mobile-autotrade-capital-set":
            return a.allocator.set_plan(
                str(payload.get("account_id") or ""),
                dict(payload.get("plan") or payload),
                actor=actor or "user")
        if command == "investment-mobile-autotrade-capital-get":
            p = a.allocator.plan(str(payload.get("account_id") or ""))
            return {"ok": p is not None, "plan": p}
        if command == "investment-mobile-autotrade-capital-check":
            account_id = str(payload.get("account_id") or "")
            return a.allocator.check_order(
                account_id, str(payload.get("strategy_id") or ""),
                instrument_id=str(payload.get("instrument_id") or ""),
                notional=payload.get("notional") or 0,
                cash=self.sim.accounts.cash(account_id),
                strategy_exposure=payload.get("strategy_exposure") or 0,
                instrument_exposure=payload.get(
                    "instrument_exposure") or 0)
        if command == "investment-mobile-autotrade-resources":
            return {"ok": True,
                    "reservations": a.resources.recent(
                        payload.get("instrument_id"))}

        if command == "investment-mobile-autotrade-job-schedule":
            return a.scheduler.schedule(
                run_id=str(payload.get("run_id") or ""),
                kind=str(payload.get("kind") or "interval"),
                market=str(payload.get("market") or ""),
                interval_s=float(payload.get("interval_s") or 0),
                at_time=str(payload.get("at_time") or ""),
                event_types=list(payload.get("event_types") or []),
                timeout_s=payload.get("timeout_s"))
        if command == "investment-mobile-autotrade-job-control":
            return a.scheduler.control(
                str(payload.get("job_id") or ""),
                str(payload.get("action") or ""))
        if command == "investment-mobile-autotrade-job-due":
            return a.scheduler.due()

        if command == "investment-mobile-autotrade-session":
            return a.sessions.session(
                str(payload.get("market") or "tw"))

        if command == "investment-mobile-autotrade-risk-check":
            run = a.manager.get(str(payload.get("run_id") or ""))
            if run is None:
                return {"ok": False, "error_code": "RUN_NOT_FOUND"}
            return a.risk_monitor.check(run)
        if command == "investment-mobile-autotrade-halt":
            return a.halt.halt(
                str(payload.get("run_id") or ""),
                str(payload.get("reason") or ""),
                detail=dict(payload.get("detail") or {}))
        if command == "investment-mobile-autotrade-recover":
            return a.recovery.recover(
                str(payload.get("run_id") or ""),
                actor=actor or "user",
                force=bool(payload.get("force")))

        if command == "investment-mobile-autotrade-performance":
            run = a.manager.get(str(payload.get("run_id") or ""))
            if run is None:
                return {"ok": False, "error_code": "RUN_NOT_FOUND"}
            return a.performance.strategy_report(run)
        if command == "investment-mobile-autotrade-performance-curve":
            return {"ok": True,
                    "curve": a.performance.curve(
                        payload.get("run_id")),
                    "max_drawdown": a.performance.max_drawdown(
                        str(payload.get("run_id") or ""))}
        if command == "investment-mobile-autotrade-stability":
            run = a.manager.get(str(payload.get("run_id") or ""))
            if run is None:
                return {"ok": False, "error_code": "RUN_NOT_FOUND"}
            return a.stability.analyze(
                run,
                recent_signals=int(payload.get("recent_signals") or 0),
                baseline_signals=int(
                    payload.get("baseline_signals") or 0),
                data_ok=bool(payload.get("data_ok", True)),
                model_ok=bool(payload.get("model_ok", True)),
                execution_errors=int(
                    payload.get("execution_errors") or 0))

        if command == "investment-mobile-autotrade-research":
            run = a.manager.get(str(payload.get("run_id") or ""))
            if run is None:
                return {"ok": False, "error_code": "RUN_NOT_FOUND"}
            return a.improvement.research(
                run, evidence=dict(payload.get("evidence") or {}))
        if command == "investment-mobile-autotrade-experiment-start":
            return a.experiments.start(
                strategy_id=str(payload.get("strategy_id") or ""),
                base_version=int(payload.get("base_version") or 1),
                candidate_parameters=dict(
                    payload.get("candidate_parameters") or {}),
                proposal_id=str(payload.get("proposal_id") or ""))
        if command == "investment-mobile-autotrade-experiment-advance":
            return a.experiments.advance(
                str(payload.get("experiment_id") or ""),
                str(payload.get("target") or ""),
                actor=actor or "user",
                evidence=dict(payload.get("evidence") or {}))
        if command == "investment-mobile-autotrade-experiment-list":
            return {"ok": True,
                    "experiments": a.experiments.list(
                        payload.get("strategy_id"))}

        if command == "investment-mobile-autotrade-overfit-record":
            return a.overfitting.record(
                strategy_id=str(payload.get("strategy_id") or ""),
                version=int(payload.get("version") or 1),
                params=dict(payload.get("params") or {}),
                dataset=payload.get("dataset") or "",
                kind=str(payload.get("kind") or "backtest"),
                metrics=dict(payload.get("metrics") or {}))
        if command == "investment-mobile-autotrade-overfit-history":
            return {"ok": True,
                    "evaluations": a.overfitting.history(
                        payload.get("strategy_id"))}

        if command == "investment-mobile-autotrade-fund-analyze":
            return a.fund_coordinator.analyze(
                str(payload.get("fund_id") or ""),
                str(payload.get("share_class_id") or "A"),
                position=payload.get("position"))

        if command == "investment-mobile-autotrade-report":
            return a.reports.generate(
                str(payload.get("report_type") or "daily"),
                runs=a.manager.list(),
                performance=[
                    a.performance.strategy_report(r)["snapshot"]
                    for r in a.manager.list()],
                shadow_signals=self.sim.shadow.signals(),
                paper_orders=self.sim.orders.list(),
                risk_events=self.monitoring.events.list(
                    event_type="risk_threshold"),
                improvements=a.improvement.list(),
                model_notes=str(payload.get("model_notes") or ""))
        if command == "investment-mobile-autotrade-report-list":
            return {"ok": True,
                    "reports": a.reports.list(
                        payload.get("report_type"))}

        if command == "investment-mobile-autotrade-maintenance-run":
            return a.maintenance.startup_recovery(a)
        if command == "investment-mobile-autotrade-maintenance-status":
            return a.maintenance.status()
        if command == "investment-mobile-autotrade-workload-stats":
            return a.workload.stats()

        return {"ok": False, "error_code": "COMMAND_UNKNOWN"}

    # ------------------------------------------------------------------
    def _handle_asset(self, command: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
        a = self.assets
        aid = str(payload.get("account_id") or "")

        if command == "investment-mobile-asset-account-list":
            return {"ok": True,
                    "accounts": a.registry.list_accounts()}
        if command == "investment-mobile-asset-account-register":
            return a.registry.register(
                str(payload.get("account_id") or ""),
                str(payload.get("label") or ""),
                str(payload.get("kind") or ""),
                broker_id=str(payload.get("broker_id") or ""),
                market=str(payload.get("market") or ""),
                currency=str(payload.get("currency") or ""))
        if command == "investment-mobile-asset-account-rename":
            return a.registry.rename(
                aid, str(payload.get("label") or ""))

        if command == "investment-mobile-asset-position-list":
            return {"ok": True,
                    "positions": a.positions.list_positions(
                        payload.get("account_id"))}
        if command == "investment-mobile-asset-position-reserve":
            return a.positions.reserve(
                aid, str(payload.get("instrument_id") or ""),
                payload.get("quantity"))
        if command == "investment-mobile-asset-position-release":
            return a.positions.release(
                aid, str(payload.get("instrument_id") or ""),
                payload.get("quantity"))

        if command == "investment-mobile-asset-transaction-record":
            return a.ledger.record(
                aid, dict(payload.get("transaction") or payload),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-transaction-correct":
            return a.ledger.correct(
                str(payload.get("transaction_id") or ""),
                dict(payload.get("fields") or {}),
                reason=str(payload.get("reason") or ""),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-transaction-list":
            return {"ok": True,
                    "transactions": a.ledger.list(
                        payload.get("account_id"),
                        payload.get("instrument_id"))}

        if command == "investment-mobile-asset-cash-deposit":
            return a.cash.deposit(
                aid, str(payload.get("currency") or ""),
                payload.get("amount"),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-cash-withdraw":
            return a.cash.withdraw(
                aid, str(payload.get("currency") or ""),
                payload.get("amount"),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-cash-adjust":
            return a.cash.adjust(
                aid, str(payload.get("currency") or ""),
                payload.get("amount"),
                str(payload.get("reason") or ""),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-cash-view":
            return a.cash.balance_view(aid)
        if command == "investment-mobile-asset-cash-reserve":
            return a.cash.reserve(
                aid, str(payload.get("currency") or ""),
                payload.get("amount"),
                str(payload.get("ref") or ""))
        if command == "investment-mobile-asset-cash-release":
            return a.cash.release(
                aid, str(payload.get("currency") or ""),
                str(payload.get("ref") or ""),
                payload.get("amount"))

        if command == "investment-mobile-asset-cost-view":
            return a.cost.position_cost(
                aid, str(payload.get("instrument_id") or ""))
        if command == "investment-mobile-asset-cost-method":
            return a.cost.set_method(
                aid, str(payload.get("method") or ""))

        if command == "investment-mobile-asset-currency-convert":
            return a.ccy.convert(
                payload.get("amount"),
                str(payload.get("from_currency") or ""),
                str(payload.get("to_currency") or ""),
                at=payload.get("at"))
        if command == "investment-mobile-asset-currency-staleness":
            return a.ccy.staleness(
                str(payload.get("base") or "USD"),
                str(payload.get("quote") or "TWD"))

        if command == "investment-mobile-asset-value":
            return a.value(
                display_currency=str(
                    payload.get("display_currency") or "TWD"),
                prices=dict(payload.get("prices") or {}))

        if command == "investment-mobile-asset-performance":
            return a.performance.pnl_summary(
                current_value=payload.get("current_value", "0"),
                invested_capital=payload.get("invested_capital", "0"),
                realized_pnl=payload.get("realized_pnl", "0"),
                unrealized_pnl=payload.get("unrealized_pnl", "0"),
                income_total=payload.get("income_total", "0"),
                fees_total=payload.get("fees_total", "0"),
                fx_cost_total=payload.get("fx_cost_total", "0"))
        if command == "investment-mobile-asset-period-pnl":
            return a.performance.period_pnl(
                list(payload.get("valuations") or
                     a.snapshots.curve()),
                str(payload.get("window") or "all"))

        if command == "investment-mobile-asset-income-record":
            return a.income.record(
                aid, dict(payload.get("income") or payload),
                source=str(payload.get("source") or "MANUAL").upper())
        if command == "investment-mobile-asset-income-list":
            return {"ok": True,
                    "income": a.income.list(payload.get("account_id"))}
        if command == "investment-mobile-asset-income-cashflow":
            return a.income.cashflow(
                window=str(payload.get("window") or "all"))
        if command == "investment-mobile-asset-income-breakdown":
            return a.income.fund_distribution_breakdown()

        if command == "investment-mobile-asset-allocation-analyze":
            return a.allocation.analyze(
                a.value(display_currency=str(
                    payload.get("display_currency") or "TWD"),
                    prices=dict(payload.get("prices") or {})),
                dict(payload.get("tags") or {}))
        if command == "investment-mobile-asset-allocation-target":
            return a.allocation.set_target(
                str(payload.get("dimension") or ""),
                str(payload.get("key") or ""),
                payload.get("weight"),
                drift_band=payload.get("drift_band", "0.05"),
                max_weight=payload.get("max_weight"),
                actor=str(payload.get("actor") or "user"))
        if command == "investment-mobile-asset-allocation-propose":
            return a.allocation.propose(
                str(payload.get("dimension") or ""),
                str(payload.get("key") or ""),
                payload.get("weight"),
                rationale=str(payload.get("rationale") or ""),
                actor=str(payload.get("actor") or "ai"))

        if command == "investment-mobile-asset-exposure-basket":
            return a.exposure.register_basket(
                str(payload.get("instrument_id") or ""),
                list(payload.get("constituents") or []),
                disclosed_at=float(payload.get("disclosed_at") or 0),
                source=str(payload.get("source") or "MANUAL"))
        if command == "investment-mobile-asset-exposure-related":
            return a.exposure.register_related(
                str(payload.get("group_id") or ""),
                list(payload.get("instrument_ids") or []))
        if command == "investment-mobile-asset-exposure-analyze":
            return a.exposure.analyze(
                a.value(display_currency=str(
                    payload.get("display_currency") or "TWD"),
                    prices=dict(payload.get("prices") or {})))

        if command == "investment-mobile-asset-snapshot-capture":
            return a.snapshots.capture(
                str(payload.get("period") or "daily"), a.value())
        if command == "investment-mobile-asset-snapshot-history":
            return {"ok": True,
                    "snapshots": a.snapshots.history(
                        str(payload.get("period") or "daily")),
                    "curve": a.snapshots.curve(
                        str(payload.get("period") or "daily"))}

        if command == "investment-mobile-asset-risk-report":
            return a.risk_analytics.report(
                a.value(), a.snapshots.curve())

        if command == "investment-mobile-asset-ai-analyze":
            val = a.value(
                display_currency=str(
                    payload.get("display_currency") or "TWD"),
                prices=dict(payload.get("prices") or {}))
            scope = str(payload.get("scope") or "total")
            if scope == "account":
                return a.intelligence.analyze_account(
                    val, aid)
            if scope == "risk":
                return a.intelligence.analyze_risk(
                    val, a.snapshots.curve())
            if scope == "overlap":
                return a.intelligence.analyze_overlap(val)
            if scope == "cashflow":
                return a.intelligence.analyze_cashflow(
                    window=str(payload.get("window") or "month"))
            return a.intelligence.analyze_total(val)

        if command == "investment-mobile-asset-recommend":
            return a.recommendations.recommend(
                a.value(display_currency=str(
                    payload.get("display_currency") or "TWD"),
                    prices=dict(payload.get("prices") or {})),
                dict(payload.get("tags") or {}))

        if command == "investment-mobile-asset-maintenance-run":
            return a.maintenance.run_all(a)
        if command == "investment-mobile-asset-maintenance-status":
            return a.maintenance.status()

        if command == "investment-mobile-asset-environment-view":
            from .assets.isolation import PortfolioEnvironment
            name = str(payload.get("environment") or
                       "MANUAL").upper()
            name = name if name.endswith("_PORTFOLIO") \
                else f"{name}_PORTFOLIO"
            try:
                env = PortfolioEnvironment(name)
            except ValueError:
                return {"ok": False,
                        "error_code": "ENVIRONMENT_UNKNOWN"}
            return a.isolation.environment_view(env)

        if command == "investment-mobile-import-diff":
            return self.importer.diff(
                str(payload.get("batch_id") or ""),
                mapping=dict(payload.get("mapping") or {}))

        return {"ok": False, "error_code": "COMMAND_UNKNOWN"}

    # ------------------------------------------------------------------
    def _handle_monitoring(self, command: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
        """Monitoring center commands — the AI actor may read, scan and
        ask for analysis, but every mutating control (alert rules, rec
        lifecycle promotion) rejects AI actors."""
        m = self.monitoring
        actor = str(payload.get("actor") or "")
        is_ai = actor.lower() in ("ai", "xingcheng", "model",
                                  "assistant")
        mutating = command in {
            "investment-mobile-monitor-alert-rule-set",
            "investment-mobile-monitor-event-resolve",
            "investment-mobile-monitor-notification-read",
            "investment-mobile-monitor-rec-transition",
            "investment-mobile-monitor-schedule-ran",
        }
        if is_ai and mutating:
            return {"ok": False, "error_code": "AI_MUTATION_DENIED"}

        if command == "investment-mobile-monitor-overview":
            return m.overview()

        if command == "investment-mobile-monitor-scan-tw":
            return m.tw_monitor.scan(
                list(payload.get("instrument_ids") or []),
                positions=list(payload.get("positions") or []))
        if command == "investment-mobile-monitor-scan-us":
            return m.us_monitor.scan(
                list(payload.get("instrument_ids") or []),
                positions=list(payload.get("positions") or []))
        if command == "investment-mobile-monitor-scan-fund":
            return m.fund_monitor.scan(
                [tuple(f) for f in payload.get("funds") or []])
        if command == "investment-mobile-monitor-session-us":
            return m.us_monitor.session_state()

        if command == "investment-mobile-monitor-events":
            return {"ok": True,
                    "events": m.events.list(
                        severity=payload.get("severity"),
                        event_type=payload.get("event_type"),
                        market=payload.get("market"),
                        status=payload.get("status"))}
        if command == "investment-mobile-monitor-event-resolve":
            return m.events.resolve(str(payload.get("event_id") or ""))

        if command == "investment-mobile-monitor-data-gate":
            scope = str(payload.get("scope") or "instrument")
            if scope == "fund":
                return m.gate.check_fund(
                    str(payload.get("fund_id") or ""),
                    str(payload.get("share_class_id") or "A"))
            if scope == "fx":
                return m.gate.check_fx(
                    str(payload.get("base") or "USD"),
                    str(payload.get("quote") or "TWD"))
            return m.gate.evaluate(
                list(payload.get("instrument_ids") or []),
                market=str(payload.get("market") or ""))

        if command == "investment-mobile-monitor-alert-rule-set":
            return m.alerts.set_rule(
                dict(payload.get("rule") or payload),
                actor=actor or "user")
        if command == "investment-mobile-monitor-alert-rules":
            return {"ok": True, "rules": m.alerts.list_rules()}
        if command == "investment-mobile-monitor-alert-evaluate":
            return m.alerts.evaluate(
                market_data=dict(payload.get("market_data") or {}),
                valuation=(self.assets.value(
                    prices=dict(payload.get("prices") or {}))
                    if payload.get("use_valuation") else
                    payload.get("valuation")),
                allocation=payload.get("allocation"),
                indicators=dict(payload.get("indicators") or {}))

        if command == "investment-mobile-monitor-notifications":
            return {"ok": True,
                    "notifications": m.notifications.history(
                        channel=payload.get("channel"),
                        severity=payload.get("severity"),
                        unread_only=bool(
                            payload.get("unread_only")))}
        if command == "investment-mobile-monitor-notification-read":
            return m.notifications.mark_read(
                str(payload.get("notification_id") or ""))

        if command == "investment-mobile-monitor-recommend":
            return m.recommend.recommend(
                instrument_id=str(payload.get("instrument_id") or ""),
                account_id=str(payload.get("account_id") or ""),
                kind=str(payload.get("kind") or "equity"),
                indicators=dict(payload.get("indicators") or {}),
                position=payload.get("position"),
                strategy_id=str(payload.get("strategy_id") or ""),
                strategy_version=str(
                    payload.get("strategy_version") or ""),
                data_timestamp=payload.get("data_timestamp"),
                nav=payload.get("nav"),
                reasoning_override=payload.get("reasoning"))
        if command == "investment-mobile-monitor-rec-list":
            return {"ok": True,
                    "recommendations": m.recommend.list(
                        payload.get("status"))}
        if command == "investment-mobile-monitor-rec-transition":
            return m.recommend.transition(
                str(payload.get("recommendation_id") or ""),
                str(payload.get("target") or ""),
                actor=actor or "user",
                reason=str(payload.get("reason") or ""))
        if command == "investment-mobile-monitor-rec-outcome":
            return m.recommend.evaluate_outcome(
                str(payload.get("recommendation_id") or ""),
                int(payload.get("horizon_days") or 30))
        if command == "investment-mobile-monitor-rec-expire":
            return m.recommend.expire_due()

        if command == "investment-mobile-monitor-scan-opportunities":
            return m.scanner.scan(
                list(payload.get("universe") or []),
                dict(payload.get("criteria") or {}))

        if command == "investment-mobile-monitor-risk-check":
            return m.risk_monitor.check(
                self.assets.value(
                    prices=dict(payload.get("prices") or {})),
                self.assets.snapshots.curve())
        if command == "investment-mobile-monitor-cross-market":
            return m.cross_market.check(
                self.assets.value(
                    prices=dict(payload.get("prices") or {})))

        if command == "investment-mobile-monitor-reallocation":
            return m.reallocation.propose(
                self.assets.value(
                    prices=dict(payload.get("prices") or {})),
                dimension=str(payload.get("dimension") or "market"),
                tags=dict(payload.get("tags") or {}),
                actor=actor or "user")
        if command == "investment-mobile-monitor-reallocation-list":
            return {"ok": True,
                    "proposals": m.reallocation.list()}

        if command == "investment-mobile-monitor-report":
            return m.reports.generate(
                str(payload.get("report_type") or "daily"),
                valuation=self.assets.value(
                    prices=dict(payload.get("prices") or {})),
                positions=self.assets.positions.list_positions(),
                income=self.assets.income.cashflow(),
                events=m.events.list(),
                recommendations=m.recommend.list(),
                allocation=None,
                risk=None,
                model_notes=str(payload.get("model_notes") or ""))
        if command == "investment-mobile-monitor-report-list":
            return {"ok": True,
                    "reports": m.reports.list(
                        payload.get("report_type"))}

        if command == "investment-mobile-monitor-schedule-due":
            return m.scheduler.due(
                dict(payload.get("market_date_fresh") or {}))
        if command == "investment-mobile-monitor-schedule-ran":
            return m.scheduler.mark_ran(
                str(payload.get("schedule_id") or ""),
                str(payload.get("market_date") or ""),
                payload.get("run_key"))

        if command == "investment-mobile-monitor-orchestrate":
            return m.orchestrator.run(
                str(payload.get("kind") or ""),
                dict(payload.get("payload") or {}),
                data_vintage=str(
                    payload.get("data_vintage") or ""),
                deterministic=payload.get("deterministic"))
        if command == "investment-mobile-monitor-orchestrator-stats":
            return m.orchestrator.stats()

        if command == "investment-mobile-monitor-maintenance-run":
            return m.maintenance.run_all(m)
        if command == "investment-mobile-monitor-maintenance-status":
            return m.maintenance.status()

        return {"ok": False, "error_code": "COMMAND_UNKNOWN"}

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

    def _handle_perf(self, command: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        """Phase-13 runtime commands. Reads are open; AI actors can never
        raise budgets, force transitions, run maintenance, or purge."""
        p = self.perf
        actor = str(payload.get("actor") or "")
        is_ai = actor.lower() in ("ai", "xingcheng", "model", "assistant")
        mutating = command in {
            "investment-mobile-perf-budget-set",
            "investment-mobile-perf-lifecycle-transition",
            "investment-mobile-perf-maintenance-run",
            "investment-mobile-perf-recovery-run",
            "investment-mobile-perf-retention-run",
            "investment-mobile-perf-cache-invalidate",
            "investment-mobile-perf-power-event",
        }
        if is_ai and mutating:
            return {"ok": False, "error_code": "AI_MUTATION_DENIED"}

        if command == "investment-mobile-perf-overview":
            return p.overview()
        if command == "investment-mobile-perf-health":
            return p.health()
        if command == "investment-mobile-perf-metrics":
            return p.metrics.snapshot()
        if command == "investment-mobile-perf-budget-status":
            return p.budget.status()
        if command == "investment-mobile-perf-budget-set":
            r = p.budget.set_limit(
                str(payload.get("key") or ""),
                int(payload.get("value") or 0),
                actor=actor or "user")
            self.audit.record("perf.budget_set", {
                **r, "actor": actor})
            return r
        if command == "investment-mobile-perf-cache-stats":
            return p.cache.stats()
        if command == "investment-mobile-perf-cache-invalidate":
            src = str(payload.get("source") or "")
            if src:
                return {"ok": True,
                        "new_revision": p.cache.bump_revision(src)}
            return {"ok": True,
                    "invalidated": p.cache.invalidate(
                        str(payload.get("key") or ""))}
        if command == "investment-mobile-perf-subscribe":
            # subscribers are in-process callbacks — the command surface
            # registers a mirror-style subscriber that enqueues updates
            sid = p.subscriptions.subscribe(
                str(payload.get("instrument_id") or ""),
                lambda u: self._mirror_outbox.append(
                    {"operation": "record_market_quote",
                     "quote": u}),
                pinned=bool(payload.get("pinned")),
                owner=str(payload.get("owner") or ""))
            return {"ok": True, "subscription_id": sid}
        if command == "investment-mobile-perf-unsubscribe":
            return {"ok": p.subscriptions.unsubscribe(
                str(payload.get("subscription_id") or ""))}
        if command == "investment-mobile-perf-sub-status":
            return p.subscriptions.status()
        if command == "investment-mobile-perf-indicator-update":
            iid = str(payload.get("instrument_id") or "")
            ind = p.indicator(iid)
            close = payload.get("close")
            if payload.get("correct") is not None:
                return ind.correct_bar(
                    int(payload.get("index_from_end") or -1),
                    float(payload.get("correct")))
            if close is None:
                return {"ok": False, "error_code": "CLOSE_REQUIRED"}
            with p.metrics.timed("indicator_update"):
                return {"ok": True, "indicator": ind.append_bar(float(close))}
        if command == "investment-mobile-perf-job-submit":
            return p.jobs.submit(
                str(payload.get("job_type") or ""),
                priority=int(payload.get("priority") or 5),
                payload=payload.get("payload"),
                timeout_s=payload.get("timeout_s"),
                idem_key=str(payload.get("idem_key") or ""))
        if command == "investment-mobile-perf-job-control":
            return p.jobs.cancel(str(payload.get("job_id") or ""))
        if command == "investment-mobile-perf-job-status":
            return p.jobs.status()
        if command == "investment-mobile-perf-inference-submit":
            return p.inference.submit(
                str(payload.get("kind") or ""),
                dict(payload.get("payload") or {}),
                priority=str(payload.get("priority") or "RESEARCH"),
                ttl_s=float(payload.get("ttl_s") or 300.0))
        if command == "investment-mobile-perf-inference-status":
            return p.inference.health()
        if command == "investment-mobile-perf-maintenance-run":
            return p.maintenance.run_tier(
                str(payload.get("tier") or "periodic"),
                force=bool(payload.get("force")))
        if command == "investment-mobile-perf-maintenance-status":
            return p.maintenance.status()
        if command == "investment-mobile-perf-recovery-run":
            return p.recovery.run(trigger=str(
                payload.get("trigger") or "manual"))
        if command == "investment-mobile-perf-recovery-history":
            return {"ok": True, "runs": p.recovery.history()}
        if command == "investment-mobile-perf-power-event":
            r = p.power.handle(str(payload.get("event") or ""))
            if str(payload.get("event") or "").lower() == "resume":
                r["resume_checks"] = p.power.resume_checks()
            self.audit.record("perf.power_event", {
                "event": payload.get("event"), "result": r.get("state")})
            return r
        if command == "investment-mobile-perf-lifecycle-status":
            return p.lifecycle.status()
        if command == "investment-mobile-perf-lifecycle-transition":
            r = p.lifecycle.transition(
                str(payload.get("to") or ""),
                reason=str(payload.get("reason") or ""), actor=actor)
            self.audit.record("perf.lifecycle", {
                **r, "actor": actor})
            return r
        if command == "investment-mobile-perf-retention-run":
            return p.retention.purge(str(payload.get("category") or ""))
        if command == "investment-mobile-perf-retention-status":
            return p.retention.status()
        if command == "investment-mobile-perf-pool-stats":
            return p.pool.stats()
        return {"ok": False, "error_code": "COMMAND_UNKNOWN"}

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
