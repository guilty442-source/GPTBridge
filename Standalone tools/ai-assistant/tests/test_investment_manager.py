"""ai-assistant consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: ai-assistant/tests/test_investment_manager.py
########################################################################
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
