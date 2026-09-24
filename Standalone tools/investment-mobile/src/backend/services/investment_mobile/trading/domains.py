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
        "module": "trading.strategy_engine.StrategyEngine",
        "contracts": "TradingSignal → TradeProposal",
        "boundary": "sole signal→proposal converter; never creates OrderRequest",
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
    "backtest": {
        "module": "trading.backtest.BacktestEngine",
        "contracts": "simulated fills",
        "boundary": "research replay; outputs always marked simulated",
    },
    "audit": {
        "module": "trading.audit.TradingAudit",
        "contracts": "audit_event",
        "boundary": "append-only journal; every pipeline stage emits evidence",
    },
}
