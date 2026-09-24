"""market-data domain — unified market data engine + financial data center.

Components:
- ``engine``      MarketDataEngine — centralized bounded ingestion,
                  subscription lifecycle, per-source fault isolation
- ``sources``     replaceable SourceAdapter interface + capability matrix
- ``calendar``    TradingCalendar — TW/US sessions, holidays, DST
- ``history``     HistoricalMarketDataService — incremental cursor sync,
                  dedup, gap detection, deterministic replay
- ``corporate``   CorporateActionService — splits/dividends/adjustments
- ``fx``          CurrencyRateService — TWD/USD with provenance
- ``quality``     quote validation + anomaly detection
- ``query``       MarketDataQuery — 星澄 read-only surface
- ``maintenance`` bounded health/integrity/resync pass
"""

from .calendar import TradingCalendar
from .contracts import (
    AdjustmentType,
    ConnectionStatus,
    DataStatus,
    MarketCandle,
    MarketDataStatus,
    MarketQuote,
    MarketSession,
    Timeframe,
)
from .corporate import CorporateAction, CorporateActionService
from .engine import MarketDataEngine
from .fx import CurrencyRateService
from .history import CandleStore, HistoricalMarketDataService
from .maintenance import MarketDataMaintenance
from .query import MarketDataQuery
from .sources import (
    SOURCE_CAPABILITIES,
    ManualImportSource,
    MarketDataSource,
    SimulatedSource,
    SourceCapability,
    SourceError,
)

__all__ = (
    "AdjustmentType",
    "CandleStore",
    "ConnectionStatus",
    "CorporateAction",
    "CorporateActionService",
    "CurrencyRateService",
    "DataStatus",
    "HistoricalMarketDataService",
    "ManualImportSource",
    "MarketCandle",
    "MarketDataEngine",
    "MarketDataMaintenance",
    "MarketDataQuery",
    "MarketDataStatus",
    "MarketDataSource",
    "MarketQuote",
    "MarketSession",
    "SOURCE_CAPABILITIES",
    "SimulatedSource",
    "SourceCapability",
    "SourceError",
    "Timeframe",
    "TradingCalendar",
)
