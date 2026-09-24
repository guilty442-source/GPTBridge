"""Unified trading contracts — 星澄 AI 投資管理與自動操盤系統.

Single canonical vocabulary for the whole pipeline:

    MarketData → Strategy → TradingSignal → TradeProposal
      → RiskEngine → OrderRequest → OrderReceipt
      → BrokerAdapter → Execution → Portfolio

Rules enforced by shape (not convention):
- 星澄 may only emit ``TradeProposal`` — it never constructs
  ``OrderRequest`` and never touches ``BrokerAdapter``.
- ``RiskDecision`` is produced exclusively by the risk engine; the
  ``risk_params`` field of a proposal is informational and is NEVER
  trusted — the engine reads limits from its own governed config.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ======================================================================
# Modes and enums
# ======================================================================

class TradingMode(str, Enum):
    """Operating modes ordered by autonomy. Default is ANALYSIS."""

    ANALYSIS = "ANALYSIS"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class InstrumentType(str, Enum):
    TW_STOCK = "TW_STOCK"
    TW_ETF = "TW_ETF"
    US_STOCK = "US_STOCK"
    US_ETF = "US_ETF"
    MUTUAL_FUND = "MUTUAL_FUND"


class BrokerId(str, Enum):
    CATHAY_SECURITIES = "CATHAY_SECURITIES"
    FUBON_SUBBROKERAGE = "FUBON_SUBBROKERAGE"
    MUTUAL_FUND_PROVIDER = "MUTUAL_FUND_PROVIDER"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SUBSCRIBE = "subscribe"   # fund 申購
    REDEEM = "redeem"         # fund 贖回


class OrderStatus(str, Enum):
    CREATED = "created"
    RISK_REJECTED = "risk_rejected"
    MODE_BLOCKED = "mode_blocked"
    ADAPTER_DENIED = "adapter_denied"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ======================================================================
# Market data
# ======================================================================

@dataclass
class MarketObservation:
    """One market data point (quote/NAV) — non-authoritative cache entry."""

    instrument_id: str
    price: float
    currency: str
    observed_at: float = field(default_factory=_now)
    source: str = ""
    kind: str = "quote"  # quote | nav
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Instrument — collision-free across market/currency/share-class
# ======================================================================

@dataclass
class FundDetails:
    fund_id: str = ""
    fund_share_class: str = ""
    fund_currency: str = ""
    distribution_type: str = ""      # accumulation | distribution
    nav: float | None = None
    nav_date: str = ""
    subscription_cutoff: str = ""    # e.g. "13:30 T+0"
    redemption_rules: dict[str, Any] = field(default_factory=dict)
    fee_schedule: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Instrument:
    instrument_type: str             # InstrumentType value
    market: str                      # tw | us | fund
    symbol: str
    currency: str
    display_name: str = ""
    isin: str = ""
    exchange: str = ""
    trading_calendar: str = ""       # e.g. TWSE / NYSE / fund-platform
    price_precision: int = 2
    quantity_precision: int = 0
    status: str = "active"           # active | suspended | closed
    fund: FundDetails | None = None
    instrument_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if not self.instrument_id:
            self.instrument_id = self.make_id()

    def make_id(self) -> str:
        """Collision-free identity: market + type + symbol (+ fund keys)."""
        symbol = str(self.symbol).strip().upper()
        if self.instrument_type == InstrumentType.MUTUAL_FUND.value and self.fund:
            share = (self.fund.fund_share_class or "NA").upper()
            fid = (self.fund.fund_id or symbol).upper()
            return f"fund:{fid}:{share}:{str(self.currency).upper()}"
        return (
            f"{str(self.market).lower()}:{self.instrument_type}:"
            f"{symbol}:{str(self.currency).upper()}"
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.fund is None:
            data.pop("fund", None)
        return data


# ======================================================================
# Broker / Account — per-broker isolation of funds, positions, orders
# ======================================================================

@dataclass
class Broker:
    broker_id: str                   # BrokerId value
    market: str                      # tw | us | fund
    label: str = ""
    api_verified: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Account:
    """One account at one broker — funds/positions/orders never mix."""

    account_id: str
    broker_id: str                   # BrokerId value
    market: str
    currency: str
    permissions: list[str] = field(default_factory=list)  # e.g. ["analysis","paper"]
    status: str = "active"           # active | suspended | closed
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CashBalance:
    account_id: str
    currency: str
    available: float = 0.0
    held: float = 0.0
    simulated: bool = True
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Trading pipeline contracts
# ======================================================================

@dataclass
class TradingSignal:
    """星澄 candidate signal — advisory only, never an order."""

    instrument_id: str
    market: str
    side: str = OrderSide.BUY.value
    confidence: float = 0.0
    price: float | None = None
    quantity: float = 0.0
    rationale: str = ""
    source: str = "xingcheng"
    signal_id: str = field(default_factory=lambda: _new_id("sig"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeProposal:
    """Formal proposal emitted by a strategy (or AI as proposal-only)."""

    instrument_id: str
    market: str
    side: str
    quantity: float
    price: float | None = None
    strategy_id: str = ""
    signal_id: str = ""
    account_id: str = ""
    notional: float = 0.0
    # Informational only — the risk engine NEVER trusts these.
    risk_params: dict[str, Any] = field(default_factory=dict)
    proposal_id: str = field(default_factory=lambda: _new_id("prop"))
    created_at: float = field(default_factory=_now)

    def effective_notional(self) -> float:
        if self.notional > 0:
            return self.notional
        return float(self.quantity) * float(self.price or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskDecision:
    """Fail-closed outcome of the risk engine."""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    limits_checked: list[str] = field(default_factory=list)
    backend: str = "python"
    evaluated_at: float = field(default_factory=_now)
    decision_id: str = field(default_factory=lambda: _new_id("risk"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRequest:
    """Order entering the OMS after a positive risk decision."""

    proposal: TradeProposal
    status: str = OrderStatus.CREATED.value
    order_id: str = field(default_factory=lambda: _new_id("ord"))
    decision_id: str = ""
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["proposal"] = self.proposal.to_dict()
        return data


@dataclass
class OrderReceipt:
    """Broker-side acknowledgment (or local receipt in PAPER)."""

    order_id: str
    broker_order_id: str = ""
    status: str = OrderStatus.SUBMITTED.value
    rejection: str = ""
    simulated: bool = True
    received_at: float = field(default_factory=_now)
    receipt_id: str = field(default_factory=lambda: _new_id("rcpt"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Execution:
    """Execution report — simulated in SHADOW/PAPER, broker-reported in LIVE."""

    order_id: str
    instrument_id: str
    market: str
    side: str
    quantity: float
    price: float
    account_id: str = ""
    commission: float = 0.0
    fees: dict[str, float] = field(default_factory=dict)
    simulated: bool = True
    execution_id: str = field(default_factory=lambda: _new_id("exec"))
    executed_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    account_id: str
    instrument_id: str
    market: str
    quantity: float = 0.0
    average_cost: float = 0.0
    last_price: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * float(self.last_price or self.average_cost)

    @property
    def notional(self) -> float:
        return self.quantity * self.average_cost

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["market_value"] = self.market_value
        return data


@dataclass
class PortfolioSnapshot:
    account_id: str
    positions: list[Position] = field(default_factory=list)
    cash: list[CashBalance] = field(default_factory=list)
    total_market_value: float = 0.0
    total_cash: float = 0.0
    taken_at: float = field(default_factory=_now)
    snapshot_id: str = field(default_factory=lambda: _new_id("snap"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["positions"] = [p.to_dict() for p in self.positions]
        data["cash"] = [c.to_dict() for c in self.cash]
        return data
