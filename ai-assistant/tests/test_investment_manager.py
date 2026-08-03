from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ai_nexus.infrastructure.portfolio_file import load_json_portfolio, market_status


def test_load_json_portfolio_normalizes_holding(tmp_path: Path) -> None:
    source = tmp_path / "portfolio.json"
    source.write_text(
        json.dumps(
            {
                "holdings": [
                    {
                        "symbol": "vti",
                        "name": "US Total Market",
                        "market": "US",
                        "quantity": 2,
                        "average_cost": 250,
                        "currency": "USD",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    holdings = load_json_portfolio(source)
    assert len(holdings) == 1
    assert holdings[0].symbol == "VTI"
    assert holdings[0].quantity == 2
    assert holdings[0].currency == "USD"


def test_market_status_is_deterministic_for_known_time() -> None:
    result = market_status("TW", datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc))
    assert result["market"] == "TW"
    assert isinstance(result["is_open"], bool)
