"""Investment Mobile Domain Layer - Core business logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class InvestmentPortfolio:
    """Investment portfolio entity."""
    id: str
    name: str
    assets: list[dict[str, Any]]
    created_at: str


@dataclass
class MarketData:
    """Market data entity."""
    symbol: str
    price: float
    timestamp: str
    source: str