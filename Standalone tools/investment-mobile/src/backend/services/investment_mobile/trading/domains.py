"""Domain registry — the 11 logical domains of the trading system.

Each entry binds a domain id to its owning module, contract types and
responsibility boundary. This map is the single declaration of domain
ownership inside the tool — duplicate ledgers/order books/position
stores outside these owners are contract violations.
"""

from __future__ import annotations

DOMAINS: dict[str, dict[str, str]] = {
    "market-data": {
        "module": "trading.market.engine.MarketDataEngine",
        "contracts": "MarketQuote, MarketCandle, MarketDataStatus",
        "boundary": "centralized bounded ingestion; sources replaceable; "
                    "independent of AI model; stale/suspect data flagged",
    },
    "instrument": {
        "module": "trading.instruments.InstrumentRegistry",
        "contracts": "Instrument, FundDetails",
        "boundary": "collision-free identity; registry mirror of PG gptbridge_trading.instrument",
    },
    "portfolio": {
        "module": "trading.portfolio_engine.PortfolioEngine",
        "contracts": "Position, PortfolioSnapshot",
        "boundary": "positions derived only from Execution ledger; per-account isolation",
    },
    "strategy": {
        "module": "trading.strategy_engine.StrategyEngine + "
                  "trading.strategy.registry.StrategyRegistry",
        "contracts": "TradingSignal → TradeProposal; StrategyDefinition, "
                     "StrategyVersionSnapshot, ParamSearchRecord",
        "boundary": "sole signal→proposal converter; never creates "
                    "OrderRequest; lifecycle DRAFT→…→LIVE_ELIGIBLE is "
                    "validation state, never execution authorization",
    },
    "risk": {
        "module": "trading.risk_engine.RiskEngine",
        "contracts": "RiskDecision",
        "boundary": "independent fail-closed evaluation; limits from governed config only",
    },
    "trading": {
        "module": "trading.oms.OrderManagementSystem",
        "contracts": "OrderRequest, OrderReceipt, Execution",
        "boundary": "fixed pipeline proposal→risk→order→receipt→execution; sole adapter caller",
    },
    "broker": {
        "module": "trading.broker.base.BrokerRegistry",
        "contracts": "Broker",
        "boundary": "per-broker adapters; inert until external api_verified artifact",
    },
    "mutual-fund": {
        "module": "trading.fund.engine.MutualFundEngine",
        "contracts": "FundIdentity, FundNAV, FundDistribution, FundFee, "
                     "FundHolding, FundTransaction, FundRecommendation, "
                     "FundStrategy",
        "boundary": "analysis + recommendations only; live fund trading "
                    "disabled; NAV≠成交價; providers verified-gated",
    },
    "ai-analysis": {
        "module": "trading.ai_boundary.SignalIntake",
        "contracts": "TradingSignal (intake)",
        "boundary": "星澄 emits proposals/signals only; never reaches risk config or brokers",
    },
    "ai-intelligence": {
        "module": "trading.intelligence.engine.InvestmentIntelligenceEngine",
        "contracts": "InvestmentRecommendation, TradeProposal(validated), "
                     "AnalysisRun, AnalysisEvidence, RecommendationVersion, "
                     "RecommendationOutcome, AnalysisSchedule, "
                     "ModelAnalysisRecord",
        "boundary": "advisory only — separated from execution; model output "
                    "is untrusted input; deterministic math only for prices/"
                    "quantities/risk; degraded when model unavailable; "
                    "news/docs can never issue commands",
    },
    "backtest": {
        "module": "trading.backtest.engine.BacktestEngine + "
                  "trading.backtest.fund_engine.FundBacktestEngine + "
                  "trading.backtest.portfolio_engine.PortfolioBacktestEngine",
        "contracts": "BacktestConfig, BacktestResult, BacktestTrade, "
                     "EquityPoint, FeeRule, FillResult, MarketRules, "
                     "BrokerCapabilityProfile",
        "boundary": "PIT-gated research replay; NAV-priced for funds; "
                    "simulated fills never reach broker/account/position "
                    "ledgers; survivorship risk flagged; stale on data "
                    "revision",
    },
    "audit": {
        "module": "trading.audit.TradingAudit",
        "contracts": "audit_event",
        "boundary": "append-only journal; every pipeline stage emits evidence",
    },
}
