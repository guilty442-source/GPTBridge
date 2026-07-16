from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


SERVICES_ROOT = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "ai-assistant"
    / "src"
    / "backend"
    / "services"
)
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from ai_nexus.investment_watch import InvestmentWatchService


def _holding(symbol: str, quantity: float) -> dict[str, Any]:
    return {
        "holding_id": f"holding-{symbol}",
        "symbol": symbol,
        "market": "US",
        "name": symbol,
        "asset_type": "STOCK",
        "quantity": quantity,
        "average_cost": 100,
        "currency": "USD",
    }


def test_network_sync_three_way_merge_preserves_manual_changes(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    original = [_holding("AAPL", 1), _holding("MSFT", 2)]
    service.repository.save_state(
        {
            "portfolio": {"holding_count": 2, "manual_revision": 1},
            "holdings": original,
        }
    )

    def manual_edit(state: dict[str, Any]) -> None:
        state["holdings"][0]["quantity"] = 5
        state["holdings"] = [state["holdings"][0], _holding("NVDA", 3)]

    service.repository.update_state(manual_edit)
    synchronized = [
        {**original[0], "web_current_price": 200, "quantity": 1},
        {**original[1], "web_current_price": 400},
    ]

    service._merge_synchronized_holdings(
        original,
        synchronized,
        sync_metadata={"market_quote_sync": {"updated_count": 2}},
        sync_owned_fields={"web_current_price"},
    )

    state = service.repository.load_state()
    holdings = {item["symbol"]: item for item in state["holdings"]}
    assert set(holdings) == {"AAPL", "NVDA"}
    assert holdings["AAPL"]["quantity"] == 5
    assert holdings["AAPL"]["web_current_price"] == 200
    assert "MSFT" not in holdings
    service.analytics_store.close()
    service.repository.close()


@pytest.mark.asyncio
async def test_mobile_sync_is_opt_in_and_defaults_to_loopback(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    await service.start()
    try:
        assert service._mobile_sync_status()["running"] is False
        _event, enabled = await service.handle(
            "investment_watch_set_mobile_sync_enabled",
            {"enabled": True, "port": 0},
        )
        assert enabled["ok"] is True
        assert enabled["mobile_sync"]["running"] is True
        assert enabled["mobile_sync"]["bind_host"] == "127.0.0.1"
        assert enabled["mobile_sync"]["local_urls"] == []

        _event, disabled = await service.handle(
            "investment_watch_set_mobile_sync_enabled",
            {"enabled": False},
        )
        assert disabled["mobile_sync"]["running"] is False
    finally:
        await service.shutdown()
