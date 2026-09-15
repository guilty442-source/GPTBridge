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
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
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
# source: ai-assistant/tests/test_investment_v3.py
########################################################################
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
