"""Data models and exceptions for the investment portfolio tooling."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class InvestmentManagerError(Exception):
    """Tool-specific error."""


class QuoteProviderError(Exception):
    """Raised when one quote provider cannot return a valid quote."""


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str = ""
    market: str = ""
    asset_type: str = ""
    quantity: float = 0.0
    average_cost: float | None = None
    currency: str = ""
    principal_amount: float | None = None
    principal_currency: str = ""
    principal_twd: float | None = None
    source_row: int | None = None
    dividend_amount_twd: float | None = None
    dividend_per_unit: float | None = None
    monthly_dividend_twd: float | None = None
    annual_dividend_yield_percent: float | None = None
    payback_rate_percent: float | None = None
    current_value_twd: float | None = None
    estimated_annual_dividend_twd: float | None = None
    estimated_weekly_dividend_twd: float | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    requested_symbol: str
    provider: str
    price: float
    currency: str = ""
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    as_of: str = ""
    market_state: str = ""
    exchange: str = ""
    raw_market: str = ""


@dataclass
class QuoteAttempt:
    provider: str
    ok: bool
    message: str


@dataclass
class QuoteContext:
    holding: Holding
    now_utc: datetime
    attempts: list[QuoteAttempt] = field(default_factory=list)
