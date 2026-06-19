from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "platform_tools"
    / "tool-mqi8uv5x-fo9f"
    / "src"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("investment_manager_main", MODULE_PATH)
assert SPEC and SPEC.loader
investment_manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = investment_manager
SPEC.loader.exec_module(investment_manager)


def test_load_portfolio_accepts_chinese_headers(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text(
        "代號,市場,股數,成本,幣別\n"
        "2330,台股,1000,600,TWD\n"
        "AAPL,美股,10,190,USD\n",
        encoding="utf-8",
    )

    holdings = investment_manager.load_portfolio(portfolio)

    assert [holding.symbol for holding in holdings] == ["2330", "AAPL"]
    assert holdings[0].market == "TW"
    assert holdings[0].quantity == 1000
    assert holdings[0].average_cost == 600
    assert holdings[1].market == "US"


def test_quote_holding_falls_back_to_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request_json(url: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if "mis.twse.com.tw" in url:
            return {"msgArray": []}
        if "finance/chart" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "2330.TW",
                                "regularMarketPrice": 700,
                                "chartPreviousClose": 690,
                                "currency": "TWD",
                                "regularMarketTime": 1781676000,
                                "marketState": "REGULAR",
                                "fullExchangeName": "Taiwan",
                            }
                        }
                    ],
                    "error": None,
                }
            }
        raise AssertionError(url)

    monkeypatch.setattr(investment_manager, "request_json", fake_request_json)
    holding = investment_manager.Holding(
        symbol="2330",
        market="TW",
        quantity=1000,
        average_cost=600,
        currency="TWD",
    )
    providers = investment_manager.provider_registry()

    quote, attempts = investment_manager.quote_holding(
        holding,
        providers,
        ["twse", "yahoo-chart"],
        investment_manager.utc_now(),
    )

    assert quote is not None
    assert quote.provider == "yahoo-chart"
    assert quote.price == 700
    assert [(attempt.provider, attempt.ok) for attempt in attempts] == [
        ("twse", False),
        ("yahoo-chart", True),
    ]


def test_watch_stops_after_market_close_without_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text("symbol,market,quantity\nAAPL,US,1\n", encoding="utf-8")

    def fake_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "timestamp": "2026-06-17T00:00:00+00:00",
            "holding_count": 1,
            "quoted_count": 1,
            "failed_quote_count": 0,
            "holdings": [],
            "totals_by_currency": {},
            "market_summary": {
                "markets": {"US": {"state": "closed", "is_open": False}},
                "open_markets": [],
                "watchable_markets": ["US"],
                "all_watchable_markets_closed": True,
            },
        }

    monkeypatch.setattr(investment_manager, "create_snapshot", fake_snapshot)
    monkeypatch.setattr(
        investment_manager.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("sleep not expected")),
    )

    report = investment_manager.run_manager(
        portfolio_file=portfolio,
        provider_order=["yahoo-chart"],
        watch=True,
        interval_seconds=60,
        max_cycles=0,
        progress_jsonl=False,
    )

    assert report["stop_reason"] == "market_closed"
    assert report["cycle_count"] == 1


def test_excel_import_returns_actionable_error(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    portfolio.write_bytes(b"not-an-xlsx")

    with pytest.raises(investment_manager.InvestmentManagerError) as exc_info:
        investment_manager.load_portfolio(portfolio)

    assert "conversion to CSV or JSON" in str(exc_info.value)


def test_market_status_falls_back_when_zoneinfo_data_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_zoneinfo(_name: str) -> object:
        raise RuntimeError("missing tzdata")

    monkeypatch.setattr(investment_manager, "ZoneInfo", fail_zoneinfo)

    status = investment_manager.market_status(
        "US",
        investment_manager.datetime.fromisoformat("2026-06-17T14:00:00+00:00"),
    )

    assert status["timezone"] == "America/New_York"
    assert status["state"] == "open"
