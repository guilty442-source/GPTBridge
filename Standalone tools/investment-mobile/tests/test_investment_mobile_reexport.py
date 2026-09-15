"""Investment Mobile Re-export Tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Add paths before imports
ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "Standalone tools" / "investment-mobile"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

import pytest


def test_investment_mobile_reexports() -> None:
    """Test that investment-mobile re-exports all public APIs."""
    from investment_mobile import (
        AnalyzeInvestmentUseCase,
        ChannelClient,
        DatabaseClient,
        ExternalAPIClient,
        InvestmentMobilePresenter,
        InvestmentPortfolio,
        ManagePortfolioUseCase,
        MarketData,
        MarketDataClient,
        PortfolioPresenter,
    )

    # Verify all expected classes are importable
    assert AnalyzeInvestmentUseCase is not None
    assert ChannelClient is not None
    assert DatabaseClient is not None
    assert ExternalAPIClient is not None
    assert InvestmentMobilePresenter is not None
    assert InvestmentPortfolio is not None
    assert ManagePortfolioUseCase is not None
    assert MarketData is not None
    assert MarketDataClient is not None
    assert PortfolioPresenter is not None