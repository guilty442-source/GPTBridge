"""Investment Mobile re-export tests — new trading engine surface."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "Standalone tools" / "investment-mobile"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))


def test_investment_mobile_reexports() -> None:
    """The public API surface is the trading engine cluster + channel."""
    from investment_mobile import (
        ChannelClient,
        ExternalAPIClient,
        Fill,
        Order,
        OrderIntent,
        Position,
        RiskDecision,
        Signal,
        TradingEngineService,
        TradingMode,
    )

    for cls in (
        ChannelClient,
        ExternalAPIClient,
        Fill,
        Order,
        OrderIntent,
        Position,
        RiskDecision,
        Signal,
        TradingEngineService,
        TradingMode,
    ):
        assert cls is not None
