from __future__ import annotations

from pathlib import Path

from ai_nexus.infrastructure.analytics_repository import InvestmentAnalyticsStore
from ai_nexus.application.portfolio_engine import InvestmentV3Engine


def test_v3_fx_rates_support_direct_and_inverse_lookup(tmp_path: Path) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    store = InvestmentAnalyticsStore(tool_root)
    try:
        engine = InvestmentV3Engine(store)
        count = engine.add_fx_rates(
            [
                {
                    "base_currency": "USD",
                    "quote_currency": "TWD",
                    "observed_at": "2026-08-12T00:00:00+00:00",
                    "rate": 32.0,
                    "provider": "test",
                    "verified": True,
                }
            ]
        )
        assert count == 1
        assert engine.fx_rate("USD", "TWD")["rate"] == 32.0
        assert engine.fx_rate("TWD", "USD")["rate"] == 1 / 32.0
    finally:
        store.close()
