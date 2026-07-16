from __future__ import annotations

import asyncio
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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

from ai_nexus.investment_analytics import (
    InvestmentAnalyticsStore,
    market_session_status,
    sync_yahoo_dividends,
    sync_yahoo_open_market_quotes,
)
from ai_nexus.investment_automation import InvestmentAutomation, ModelGovernance, NotificationManager
from ai_nexus.investment_broker import BrokerReconciliationService
from ai_nexus.investment_privacy import decode_binary_document
from ai_nexus.investment_v3 import (
    InvestmentV3Engine,
    sync_factor_proxies_from_yahoo,
    sync_fx_from_huanan_bank,
)
from ai_nexus.investment_watch import InvestmentWatchService
import ai_nexus.investment_watch as investment_watch_module


def state() -> dict[str, Any]:
    return {
        "portfolio": {"file_name": "portfolio.csv", "holding_count": 2},
        "holdings": [
            {"symbol": "AAPL", "market": "US", "asset_type": "STOCK", "quantity": 10, "average_cost": 100, "currency": "USD"},
            {"symbol": "MSFT", "market": "US", "asset_type": "STOCK", "quantity": 5, "average_cost": 200, "currency": "USD"},
        ],
    }


def seed_history(store: InvestmentAnalyticsStore, days: int = 140) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    symbols = ["AAPL", "MSFT", "SPY", "IWM", "IWD", "IWF", "MTUM", "QUAL"]
    bars = []
    for index in range(days):
        for offset, symbol in enumerate(symbols):
            base = 90 + offset * 22
            close = base + index * (0.18 + offset * 0.015) + math.sin(index / (3.5 + offset)) * (1 + offset / 8)
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": (start + timedelta(days=index)).isoformat(),
                    "close": close,
                    "currency": "USD",
                    "provider": "v3-test",
                    "verified": True,
                }
            )
    store.add_price_bars(bars)


def test_database_is_encrypted_at_rest_and_supports_backup_restore_rotation(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    legacy = runtime / "investment_analytics_v2.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    connection.execute("INSERT INTO legacy_marker VALUES('preserved')")
    connection.commit()
    connection.close()

    store = InvestmentAnalyticsStore(tmp_path)
    raw = legacy.read_bytes()
    decoded, envelope = decode_binary_document(raw)

    assert not raw.startswith(b"SQLite format 3\x00")
    assert decoded.startswith(b"SQLite format 3\x00")
    assert envelope["purpose"] == "investment-analytics-database-v3"
    with store.connect() as active:
        assert active.execute("SELECT value FROM legacy_marker").fetchone()[0] == "preserved"
    assert any("pre-schema-" in item["name"] for item in store.list_backups())

    backup = store.backup_database("unit-test")
    assert Path(backup["manifest_path"]).exists()
    listed = next(
        item for item in store.list_backups() if item["name"] == Path(backup["path"]).name
    )
    assert listed["integrity_verified"] is True
    store.add_transaction({"side": "BUY", "symbol": "AAPL", "quantity": 1, "price": 100})
    assert store.ledger_summary()["transaction_count"] == 1
    store.restore_database(Path(backup["path"]).name)
    assert store.ledger_summary()["transaction_count"] == 0
    previous = store.database_security_status()["key_id"]
    rotation = store.rotate_database_protection()
    assert rotation["key_id"] != previous
    assert store.database_security_status()["encrypted_at_rest"] is True


def test_historical_fx_accounting_separates_asset_and_currency_pnl(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    store.set_setting("base_currency", "TWD")
    store.add_transaction(
        {
            "side": "BUY",
            "symbol": "AAPL",
            "quantity": 10,
            "price": 100,
            "currency": "USD",
            "occurred_at": "2025-01-01T00:00:00Z",
        }
    )
    store.add_price_bars([{"symbol": "AAPL", "observed_at": "2025-12-31T00:00:00Z", "close": 120}])
    assert engine.add_fx_rates(
        [
            {"base": "USD", "quote": "TWD", "observed_at": "2025-01-01", "rate": 30},
            {"base": "USD", "quote": "TWD", "observed_at": "2025-12-31", "rate": 32},
        ]
    ) == 2

    result = engine.multi_currency_accounting({"holdings": [state()["holdings"][0]]})

    assert result["market_value_base"] == 38400
    assert result["cost_value_base"] == 30000
    assert result["asset_pnl_base"] == 6000
    assert result["currency_pnl_base"] == 2400
    assert result["total_pnl_base"] == 8400


def test_optimization_factor_regime_and_monte_carlo_are_bounded(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    seed_history(store)

    for method in ("risk_parity", "minimum_variance", "max_sharpe", "black_litterman", "cvar"):
        result = engine.optimize_portfolio(
            state(), method=method, max_position_percent=70, max_turnover_percent=60,
            views={"AAPL": 12}, seed=9,
        )
        assert result["ok"] is True
        assert abs(sum(result["weights"].values()) - 100) <= 0.02
        assert max(result["weights"].values()) <= 70.01
        assert result["turnover_percent"] <= 60.02
        assert result["execution_policy"] == "simulation_only_human_approval_required"

    first = engine.monte_carlo(state(), simulations=250, horizon_days=30, seed=77)
    second = engine.monte_carlo(state(), simulations=250, horizon_days=30, seed=77)
    assert first["terminal_return_percent"] == second["terminal_return_percent"]
    assert first["simulations"] == 250
    assert engine.regime_detection(state())["status"] == "ready"
    factors = engine.factor_attribution(state())
    assert factors["status"] == "ready"
    assert factors["sample_count"] >= 30
    assert factors["factors"]


def test_v3_analysis_does_not_silently_truncate_positions_after_twenty(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    holdings = []
    bars = []
    for symbol_index in range(25):
        symbol = f"ASSET{symbol_index:02d}"
        holdings.append(
            {
                "symbol": symbol,
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
        )
        for day in range(40):
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": (start + timedelta(days=day)).isoformat(),
                    "close": 100 + symbol_index + day * (0.1 + symbol_index / 1000),
                    "provider": "coverage-fixture",
                    "verified": True,
                }
            )
    store.add_price_bars(bars)

    result = engine.regime_detection({"holdings": holdings})

    assert result["status"] == "ready"
    assert result["analysis_coverage"]["position_count"] == 25
    assert result["analysis_coverage"]["analyzed_symbol_count"] == 25
    assert result["analysis_coverage"]["position_cap_applied"] is False
    assert result["analysis_coverage"]["market_value_percent"] == 100


def test_factor_proxy_sync_populates_every_required_series(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    timestamps = [int((datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)).timestamp()) for index in range(35)]

    def fetch(_url: str) -> dict[str, Any]:
        return {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "indicators": {"quote": [{"close": [100 + index for index in range(35)]}]},
                    }
                ]
            }
        }

    result = sync_factor_proxies_from_yahoo(engine, fetch_json=fetch)

    assert result["factor_errors"] == []
    assert set(result["factor_symbols"]) == {"SPY", "IWM", "IWD", "IWF", "MTUM", "QUAL"}
    assert result["factor_prices_added"] == 210


def test_huanan_bank_fx_sync_uses_spot_midpoint_and_twd_base(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    html = """
    <div class="data-time">資料生效時間：2026/07/22 13:19:03</div>
    <table><tbody>
      <tr><td class="first">美金</td><td class="textR">32.375000</td><td class="textR">32.475000</td></tr>
      <tr><td class="first">日幣</td><td class="textR">0.215000</td><td class="textR">0.219000</td></tr>
    </tbody></table>
    """

    result = sync_fx_from_huanan_bank(
        engine,
        ["USD", "JPY", "TWD"],
        "TWD",
        fetch_text=lambda _url: html,
    )

    assert result["fx_provider"] == "Hua Nan Commercial Bank"
    assert result["fx_rate_type"] == "spot_midpoint"
    assert result["fx_rates_added"] == 2
    assert engine.fx_rate("USD", "TWD")["rate"] == 32.425
    assert engine.fx_rate("JPY", "TWD")["rate"] == 0.217


def test_online_dividend_sync_returns_trailing_distribution_data() -> None:
    timestamp = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())

    def fetch(_url: str) -> dict[str, Any]:
        return {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "regularMarketPrice": 100,
                            "longName": "Apple Inc.",
                            "instrumentType": "EQUITY",
                            "fullExchangeName": "NasdaqGS",
                        },
                        "events": {
                            "dividends": {
                                str(timestamp): {"date": timestamp, "amount": 1.25}
                            }
                        },
                    }
                ]
            }
        }

    result = sync_yahoo_dividends(
        [
            {"symbol": "AAPL", "market": "US", "quantity": 2, "currency": "USD"},
            {"symbol": "FUND-R003", "market": "FUND", "quantity": 2},
            {"symbol": "MSFT", "market": "US", "quantity": 0, "currency": "USD"},
        ],
        fetch_json=fetch,
        max_workers=2,
    )

    assert result["requested_count"] == 1
    assert result["updated_count"] == 1
    assert result["error_count"] == 0
    assert result["updates"][0]["trailing_annual_dividend_per_unit"] == 1.25
    assert result["updates"][0]["annual_dividend_yield_percent"] == 1.25
    assert result["updates"][0]["dividend_frequency"] == "unknown"
    assert result["updates"][0]["name"] == "Apple Inc."
    assert result["updates"][0]["instrument_type"] == "EQUITY"
    assert result["updates"][0]["exchange_name"] == "NasdaqGS"
    assert result["updates"][0]["current_price"] == 100


def test_online_dividend_sync_infers_monthly_frequency() -> None:
    now = datetime.now(timezone.utc)
    dividends = {
        str(index): {
            "date": int((now - timedelta(days=30 * index)).timestamp()),
            "amount": 0.5,
        }
        for index in range(1, 6)
    }

    def fetch(_url: str) -> dict[str, Any]:
        return {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD", "regularMarketPrice": 100},
                        "events": {"dividends": dividends},
                    }
                ]
            }
        }

    result = sync_yahoo_dividends(
        [{"symbol": "DIV", "market": "US", "quantity": 2, "currency": "USD"}],
        fetch_json=fetch,
    )

    update = result["updates"][0]
    assert update["dividend_frequency"] == "monthly"
    assert update["dividend_frequency_label"] == "每月"
    assert update["dividend_frequency_per_year"] == 12


def test_dividend_sync_converts_fund_principal_without_yahoo_symbol(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = InvestmentWatchService(tmp_path)
    service.repository.save_portfolio(
        tmp_path / "fund.xlsx",
        [
            {
                "symbol": "FUND-R003",
                "name": "美元基金",
                "market": "FUND",
                "asset_type": "FUND",
                "quantity": 10,
                "average_cost": 100,
                "currency": "USD",
                "principal_amount": 1000,
                "principal_currency": "USD",
            }
        ],
    )
    service.v3.add_fx_rates(
        [
            {
                "base": "USD",
                "quote": "TWD",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "rate": 32,
                "provider": "test",
                "verified": True,
            }
        ]
    )
    monkeypatch.setattr(
        investment_watch_module,
        "sync_fx_from_huanan_bank",
        lambda *_args, **_kwargs: {"fx_provider": "test"},
    )
    monkeypatch.setattr(
        investment_watch_module,
        "sync_yahoo_dividends",
        lambda *_args, **_kwargs: {
            "provider": "test",
            "requested_count": 0,
            "updated_count": 0,
            "error_count": 0,
            "updates": [],
            "errors": [],
        },
    )

    _event, result = asyncio.run(
        service.handle("investment_watch_sync_dividends", {})
    )

    holding = result["state"]["holdings"][0]
    assert holding["principal_amount"] == 1000
    assert holding["principal_currency"] == "USD"
    assert holding["principal_twd"] == 32000


def test_dividend_sync_batches_all_market_events(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = InvestmentWatchService(tmp_path)
    service.repository.save_portfolio(
        tmp_path / "portfolio.xlsx",
        [
            {
                "symbol": "2330",
                "name": "TSMC",
                "market": "TW",
                "asset_type": "STOCK",
                "quantity": 10,
                "average_cost": 600,
                "currency": "TWD",
            }
        ],
    )
    monkeypatch.setattr(
        investment_watch_module,
        "sync_fx_from_huanan_bank",
        lambda *_args, **_kwargs: {"fx_provider": "test"},
    )
    market_events = [
        {
            "occurred_at": f"2026-{month:02d}-01T00:00:00+00:00",
            "amount_per_unit": float(month),
        }
        for month in range(1, 13)
    ]
    monkeypatch.setattr(
        investment_watch_module,
        "sync_yahoo_dividends",
        lambda *_args, **_kwargs: {
            "provider": "test",
            "requested_count": 1,
            "updated_count": 1,
            "error_count": 0,
            "updates": [
                {
                    "market": "TW",
                    "symbol": "2330",
                    "source": "test",
                    "source_url": "",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "ok",
                    "currency": "TWD",
                    "events": market_events,
                }
            ],
            "errors": [],
        },
    )
    batches: list[list[dict[str, Any]]] = []
    original_add_events = service.analytics_store.add_events

    def tracked_add_events(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        batches.append(list(payloads))
        return original_add_events(payloads)

    monkeypatch.setattr(service.analytics_store, "add_events", tracked_add_events)

    _event, result = asyncio.run(
        service.handle("investment_watch_sync_dividends", {})
    )

    assert result["ok"] is True
    assert len(batches) == 1
    assert len(batches[0]) == 12
    assert len(service.analytics_store.list_events()) == 12


def test_market_sessions_use_each_exchange_timezone_and_lunch_break() -> None:
    us_open = market_session_status(datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc))
    assert us_open["open_markets"] == ["US"]

    asia_open = market_session_status(datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc))
    assert asia_open["markets"]["TW"]["is_open"] is True
    assert asia_open["markets"]["HK"]["is_open"] is True

    hong_kong_lunch = market_session_status(
        datetime(2026, 7, 22, 4, 30, tzinfo=timezone.utc)
    )
    assert hong_kong_lunch["markets"]["TW"]["is_open"] is True
    assert hong_kong_lunch["markets"]["HK"]["is_open"] is False

    weekend = market_session_status(datetime(2026, 7, 25, 14, 0, tzinfo=timezone.utc))
    assert weekend["open_markets"] == []


def test_open_market_quote_sync_only_fetches_active_market_holdings(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    timestamp = int(datetime(2026, 7, 22, 14, 1, tzinfo=timezone.utc).timestamp())
    requested_urls: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        requested_urls.append(url)
        return {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [timestamp],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [199.0],
                                    "high": [201.0],
                                    "low": [198.5],
                                    "close": [200.0],
                                    "volume": [1200],
                                }
                            ]
                        },
                    }
                ]
            }
        }

    result = sync_yahoo_open_market_quotes(
        store,
        [
            {"symbol": "AAPL", "market": "US", "quantity": 2, "currency": "USD"},
            {"symbol": "AAPL", "market": "US", "quantity": 2, "currency": "USD"},
            {"symbol": "MSFT", "market": "US", "quantity": 0, "currency": "USD"},
            {"symbol": "2330", "market": "TW", "quantity": 1, "currency": "TWD"},
            {"symbol": "FUND-1", "market": "FUND", "quantity": 1},
        ],
        ["US"],
        fetch_json=fetch,
    )

    assert result["requested_count"] == 1
    assert result["updated_count"] == 1
    assert result["quotes"][0]["current_price"] == 200.0
    assert len(requested_urls) == 1
    assert "AAPL" in requested_urls[0]
    assert store.latest_prices()["AAPL"]["close"] == 200.0


def test_broker_reconciliation_only_posts_confirmed_differences(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    service = BrokerReconciliationService(store)
    store.add_transaction(
        {"side": "BUY", "symbol": "AAPL", "quantity": 1, "price": 100, "occurred_at": "2025-01-02"}
    )
    statement = tmp_path / "broker.csv"
    statement.write_text(
        "交易日期,股票代號,買賣別,數量,成交價,手續費,幣別\n"
        "2025-01-02,AAPL,買進,1,100,1,USD\n"
        "2025-01-03,MSFT,買進,2,200,2,USD\n",
        encoding="utf-8-sig",
    )

    imported = service.import_statement(statement, broker="Test Broker")

    assert imported["matched_count"] == 1
    assert imported["difference_count"] == 1
    assert store.ledger_summary()["transaction_count"] == 1
    difference = next(row for row in imported["rows"] if row["match_status"] == "unmatched")
    approved = service.approve_rows(imported["import_id"], [difference["row_id"]], confirmed=True)
    assert approved["status"] == "approved"
    assert store.ledger_summary()["transaction_count"] == 2


def test_broker_approval_rolls_back_all_rows_when_one_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    service = BrokerReconciliationService(store)
    statement = tmp_path / "broker-batch.csv"
    statement.write_text(
        "date,symbol,side,quantity,price,fee,currency\n"
        "2025-01-02,AAPL,BUY,1,100,1,USD\n"
        "2025-01-03,MSFT,BUY,2,200,2,USD\n",
        encoding="utf-8-sig",
    )
    imported = service.import_statement(statement, broker="Test Broker")
    row_ids = [row["row_id"] for row in imported["rows"]]
    original_add_transaction = store.add_transaction
    call_count = 0

    def fail_second_transaction(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("synthetic broker batch failure")
        return original_add_transaction(payload)

    monkeypatch.setattr(
        store,
        "add_transaction",
        fail_second_transaction,
    )
    with pytest.raises(OSError, match="synthetic broker batch failure"):
        service.approve_rows(
            imported["import_id"],
            row_ids,
            confirmed=True,
        )

    assert store.list_transactions() == []
    after = service.get_import(imported["import_id"])
    assert all(row["match_status"] == "unmatched" for row in after["rows"])
    store.close()


def test_broker_text_pdf_is_parsed_without_holding_the_source_open(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    service = BrokerReconciliationService(store)
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(
        b"%PDF-1.4\nstream\nBT (2025-01-03 MSFT BUY 2 200 400) Tj ET\nendstream\n%%EOF"
    )

    result = service.import_statement(statement)
    statement.rename(tmp_path / "statement-renamed.pdf")

    assert result["row_count"] == 1
    assert result["difference_count"] == 1


def test_corporate_action_governance_requires_review(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    engine = InvestmentV3Engine(store)
    action = engine.add_corporate_action(
        {"symbol": "AAPL", "action_type": "split", "effective_at": "2025-03-01", "source": "test"}
    )
    assert action["status"] == "needs_correction"
    assert action["issues"]
    assert engine.corporate_action_governance()["pending_count"] == 1
    assert engine.review_corporate_action(action["action_id"], "rejected") is True
    assert engine.corporate_action_governance()["pending_count"] == 0


def test_notifications_model_guardrail_and_scheduler_are_conservative(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    notifications = NotificationManager(store)
    governance = ModelGovernance(store)

    channels = notifications.list_channels()
    assert next(item for item in channels if item["channel_id"] == "mobile_outbox")["enabled"] is True
    assert next(item for item in channels if item["channel_id"] == "email_smtp")["enabled"] is False
    try:
        notifications.configure("email_smtp", {"enabled": True, "config": {}}, confirmed=False)
        raise AssertionError("external notification must require consent")
    except ValueError:
        pass
    notifications.queue("測試", "本機佇列")
    assert notifications.dispatch() == {"processed": 1, "sent": 1, "failed": 0}
    assert notifications.outbox()[0]["status"] == "available"

    for index in range(3):
        governance.record_run(
            model_name="risk-model", version="1", inputs={"i": index}, outputs={"score": 0.9},
            metrics={
                "calibration": {
                    "evaluation_sample_id": f"small-sample-v{index}",
                    "evaluated_count": 10,
                    "brier_score": 0.5,
                    "slices": [],
                }
            },
        )
    assert governance.dashboard()["status"] == "ready"
    for index in range(5):
        governance.record_run(
            model_name="risk-model",
            version="1",
            inputs={"large": index},
            outputs={"score": 0.9},
            metrics={
                "calibration": {
                    "evaluation_sample_id": "qualified-sample-v1",
                    "evaluated_count": 30,
                    "brier_score": 0.5,
                    "slices": [],
                }
            },
        )
    governance_status = governance.dashboard()
    assert governance_status["status"] == "read_only"
    assert governance_status["calibration_sample_count"] == 4
    assert governance_status["evaluated_run_count"] == 1

    calls = 0

    async def cycle() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    with store.connect() as connection:
        connection.execute(
            "INSERT INTO scheduler_runs VALUES(?, 'investment_intelligence', ?, '', 'running', '')",
            ("interrupted-fixture", "2025-01-01T00:00:00+00:00"),
        )

    async def exercise() -> None:
        automation = InvestmentAutomation(store, notifications, cycle)
        recovered_run = next(
            item
            for item in automation.status()["runs"]
            if item["run_id"] == "interrupted-fixture"
        )
        assert recovered_run["status"] == "interrupted"
        await automation.start()
        result = await automation.run_once()
        assert result["status"] == "completed"
        assert automation.status()["running"] is True
        await automation.stop()
        assert automation.status()["running"] is False

    asyncio.run(exercise())
    assert calls == 1


def test_service_exposes_v3_commands(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    commands = {
        "investment_watch_get_v3",
        "investment_watch_add_fx_rates",
        "investment_watch_import_broker_statement",
        "investment_watch_approve_broker_rows",
        "investment_watch_optimize_portfolio",
        "investment_watch_run_monte_carlo",
        "investment_watch_add_corporate_action",
        "investment_watch_review_corporate_action",
        "investment_watch_configure_scheduler",
        "investment_watch_run_scheduler",
        "investment_watch_configure_notification",
        "investment_watch_dispatch_notifications",
        "investment_watch_record_model_evaluation",
        "investment_watch_create_backup",
        "investment_watch_restore_backup",
        "investment_watch_rotate_database_key",
        "investment_watch_upsert_holding",
        "investment_watch_delete_holding",
        "investment_watch_preview_excel_mapping",
        "investment_watch_import_excel_mapping",
        "investment_watch_sync_dividends",
    }
    assert all(service.owns(command) for command in commands)

    async def exercise() -> None:
        _event, response = await service.handle(
            "investment_watch_add_fx_rates",
            {"rates": [{"base": "USD", "quote": "TWD", "observed_at": "2025-01-01", "rate": 32}]},
        )
        assert response["ok"] is True
        _event, backup = await service.handle("investment_watch_create_backup", {"label": "service-test"})
        assert backup["backup"]["encrypted"] is True

    asyncio.run(exercise())


def test_manual_holding_crud_is_local_validated_and_audited(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    source = tmp_path / "portfolio.csv"
    original = "symbol,quantity,average_cost\nAAPL,1,100\n"
    source.write_text(original, encoding="utf-8")
    initial = service.repository.save_portfolio(
        source,
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
        ],
    )
    assert initial["holdings"][0]["holding_id"]

    async def exercise() -> None:
        _event, created = await service.handle(
            "investment_watch_upsert_holding",
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 2,
                "average_cost": 200,
                "currency": "USD",
                "refresh_quotes": False,
            },
        )
        assert created["ok"] is True
        assert created["source_file_modified"] is False
        assert len(created["state"]["holdings"]) == 2
        holding_id = created["holding"]["holding_id"]

        _event, updated = await service.handle(
            "investment_watch_upsert_holding",
            {
                **created["holding"],
                "holding_id": holding_id,
                "quantity": 3.5,
                "average_cost": 210,
                "refresh_quotes": False,
            },
        )
        assert updated["holding"]["quantity"] == 3.5
        assert updated["state"]["portfolio"]["manual_revision"] == 2

        _event, duplicate = await service.handle(
            "investment_watch_upsert_holding",
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 1,
                "average_cost": 1,
                "currency": "USD",
                "refresh_quotes": False,
            },
        )
        assert duplicate["ok"] is False

        _event, unconfirmed = await service.handle(
            "investment_watch_delete_holding",
            {"holding_id": holding_id},
        )
        assert unconfirmed["ok"] is False
        _event, deleted = await service.handle(
            "investment_watch_delete_holding",
            {"holding_id": holding_id, "confirmed": True},
        )
        assert deleted["ok"] is True
        assert len(deleted["state"]["holdings"]) == 1

    asyncio.run(exercise())
    assert source.read_text(encoding="utf-8") == original
    renamed = tmp_path / "portfolio-renamed.csv"
    source.rename(renamed)
    actions = {item["action"] for item in service.analytics_store.list_audit_log()}
    assert "holding_manually_updated" in actions
    assert "holding_manually_deleted" in actions
    service.analytics_store.close()
