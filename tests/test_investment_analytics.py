from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
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
    SCHEMA_VERSION,
    InvestmentAnalyticsUpgradeRequired,
    InvestmentAnalyticsStore,
    fetch_json_with_retry,
)
from ai_nexus.investment_automation import (
    InvestmentAutomation,
    NotificationManager,
)
from ai_nexus.investment_privacy import decode_json_document, encode_json_document
from ai_nexus.investment_repository import (
    InvestmentStateRecoveryRequired,
    InvestmentStateUpgradeRequired,
    InvestmentWatchRepository,
)
from ai_nexus.investment_contract import (
    INVESTMENT_APP_VERSION,
    INVESTMENT_STATE_SCHEMA_VERSION,
)
from ai_nexus.investment_watch import InvestmentWatchService
import ai_nexus.investment_analytics as investment_analytics_module
import ai_nexus.investment_repository as investment_repository_module
import ai_nexus.investment_watch as investment_watch_module


def sample_state() -> dict[str, Any]:
    return {
        "portfolio": {"file_name": "portfolio.csv", "holding_count": 2},
        "holdings": [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 10,
                "average_cost": 100,
                "currency": "USD",
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 5,
                "average_cost": 200,
                "currency": "USD",
            },
        ],
    }


def seed_prices(store: InvestmentAnalyticsStore, days: int = 100) -> int:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for index in range(days):
        observed_at = start + timedelta(days=index)
        values = {
            "AAPL": 100 + index * 0.7 + math.sin(index / 4),
            "MSFT": 200 + index * 0.4 + math.cos(index / 3) * 2,
            "SPY": 400 + index * 0.8 + math.sin(index / 6) * 3,
        }
        for symbol, close in values.items():
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": observed_at.isoformat(),
                    "open": close - 0.5,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 1_000_000 + index,
                    "currency": "USD",
                    "provider": "test-history",
                    "verified": True,
                }
            )
    return store.add_price_bars(bars)


def test_analysis_snapshot_merges_duplicate_symbols(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    observed_at = datetime.now(timezone.utc).isoformat()
    analysis = {
        "generated_at": observed_at,
        "holdings": [
            {
                "symbol": "AAPL",
                "currency": "USD",
                "trusted_quote": True,
                "quote": {
                    "price": 200,
                    "currency": "USD",
                    "provider": "test",
                    "as_of": observed_at,
                },
            }
        ],
    }
    state = {
        "holdings": [
            {
                "symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 2,
                "average_cost": 150,
                "currency": "USD",
            },
            {
                "symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 3,
                "average_cost": 160,
                "currency": "USD",
            },
        ]
    }

    result = store.record_analysis_snapshot(analysis, state)

    assert result["snapshot_saved"] is True
    assert result["position_count"] == 1
    assert result["quoted_position_count"] == 1
    with store.connect() as connection:
        position = connection.execute(
            "SELECT quantity, market_value, cost_value FROM snapshot_positions"
        ).fetchone()
    assert tuple(position) == (5.0, 1000.0, 780.0)


def test_transaction_ledger_fifo_and_sensitive_note(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    store.add_transaction(
        {"side": "BUY", "symbol": "AAPL", "quantity": 10, "price": 100, "fee": 2, "note": "private buy"}
    )
    store.add_transaction(
        {"side": "BUY", "symbol": "AAPL", "quantity": 5, "price": 120, "fee": 1}
    )
    store.add_transaction(
        {"side": "SELL", "symbol": "AAPL", "quantity": 8, "price": 130, "tax": 4}
    )
    store.add_transaction(
        {"side": "DIVIDEND", "symbol": "AAPL", "amount": 25}
    )

    ledger = store.ledger_summary()
    transactions = store.list_transactions()

    assert ledger["transaction_count"] == 4
    assert ledger["realized_pnl"] == 236
    assert ledger["dividend_income"] == 25
    assert ledger["fees_and_taxes"] == 7
    assert transactions[-1]["note"] == "private buy"
    assert all("note_encrypted" not in item for item in transactions)
    assert store.delete_transaction(transactions[0]["transaction_id"]) is True
    assert store.ledger_summary()["transaction_count"] == 3


def test_encrypted_database_batches_multiple_writes_into_one_persist(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    persist_count = 0
    original_persist = store._persist_database

    def tracked_persist(*, rotate_key: bool = False) -> None:
        nonlocal persist_count
        persist_count += 1
        original_persist(rotate_key=rotate_key)

    store._persist_database = tracked_persist  # type: ignore[method-assign]
    with store.batch_updates():
        store.set_setting("batch-a", {"value": 1})
        store.set_setting("batch-b", {"value": 2})

    assert persist_count == 1
    assert store.get_setting("batch-a")["value"] == 1
    assert store.get_setting("batch-b")["value"] == 2


def test_encrypted_database_batch_rolls_back_every_write_on_error(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    original_database = store.database_path.read_bytes()

    with pytest.raises(RuntimeError, match="synthetic batch failure"):
        with store.batch_updates():
            store.set_setting("batch-a", {"value": 1})
            store.set_setting("batch-b", {"value": 2})
            raise RuntimeError("synthetic batch failure")

    assert store.get_setting("batch-a", None) is None
    assert store.get_setting("batch-b", None) is None
    assert store.database_path.read_bytes() == original_database


def test_failed_encrypted_persist_restores_last_durable_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    original_database = store.database_path.read_bytes()
    original_persist = store._persist_database

    def fail_persist(*, rotate_key: bool = False) -> None:
        del rotate_key
        raise OSError("synthetic durable write failure")

    monkeypatch.setattr(store, "_persist_database", fail_persist)
    with pytest.raises(OSError, match="synthetic durable write failure"):
        store.set_setting("must-not-survive", {"value": 1})

    assert store.get_setting("must-not-survive", None) is None
    assert store.database_path.read_bytes() == original_database
    monkeypatch.setattr(store, "_persist_database", original_persist)
    store.close()


def test_destructor_releases_resources_without_shutdown_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    persist_calls = 0

    def forbidden_persist(*, rotate_key: bool = False) -> None:
        del rotate_key
        nonlocal persist_calls
        persist_calls += 1
        raise AssertionError("destructor must not persist encrypted state")

    monkeypatch.setattr(store, "_persist_database", forbidden_persist)

    store.__del__()

    assert persist_calls == 0
    assert store._closed is True
    replacement = InvestmentAnalyticsStore(tmp_path)
    replacement.close()


def test_add_events_persists_encrypted_database_once(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    persist_count = 0
    original_persist = store._persist_database

    def tracked_persist(*, rotate_key: bool = False) -> None:
        nonlocal persist_count
        persist_count += 1
        original_persist(rotate_key=rotate_key)

    store._persist_database = tracked_persist  # type: ignore[method-assign]
    payloads = [
        {
            "event_type": "dividend",
            "symbol": "AAPL",
            "title": f"AAPL dividend {index}",
            "scheduled_at": f"2026-01-{index + 1:02d}",
            "dedupe_key": f"batch-dividend-{index}",
        }
        for index in range(25)
    ]
    events = store.add_events(payloads)

    assert len(events) == 25
    assert persist_count == 1
    assert len(store.list_events()) == 25

    duplicate_events = store.add_events(payloads)

    assert len(duplicate_events) == 25
    assert persist_count == 1
    assert len(store.list_events()) == 25


def test_ledger_reconciliation_detects_and_applies_quantity_gap(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    state = sample_state()
    store.add_transaction(
        {
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "symbol": "AAPL",
            "market": "US",
            "asset_type": "STOCK",
            "side": "BUY",
            "quantity": 8,
            "price": 100,
            "currency": "USD",
        }
    )
    store.add_transaction(
        {
            "occurred_at": "2025-01-01T00:00:00+00:00",
            "symbol": "MSFT",
            "market": "US",
            "asset_type": "STOCK",
            "side": "BUY",
            "quantity": 5,
            "price": 200,
            "currency": "USD",
        }
    )

    reconciliation = store.reconcile_ledger_holdings(state)
    assert reconciliation["difference_count"] == 1
    assert reconciliation["differences"][0]["symbol"] == "AAPL"
    assert reconciliation["differences"][0]["suggestion"]["quantity"] == 2

    applied = store.apply_ledger_reconciliation(state, confirmed=True)
    assert applied["applied_count"] == 1
    assert applied["difference_count"] == 0
    assert any(
        item["source"] == "ledger_reconciliation_estimate"
        for item in store.list_transactions()
    )


def test_opening_ledger_uses_twd_principal_and_marks_estimates(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    state = {
        "portfolio": {"file_name": "mixed-assets.xlsx", "holding_count": 4},
        "holdings": [
            {
                "holding_id": "us-aapl",
                "symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 2,
                "average_cost": 100,
                "currency": "USD",
                "principal_twd": 6500,
                "web_current_value_twd": 7000,
            },
            {
                "holding_id": "tw-2330",
                "symbol": "2330",
                "market": "TW",
                "asset_type": "STOCK",
                "quantity": 10,
                "average_cost": 100,
                "currency": "TWD",
                "principal_twd": 1000,
                "current_value_twd": 1100,
            },
            {
                "holding_id": "us-missing",
                "symbol": "MISS",
                "market": "US",
                "asset_type": "ETF",
                "quantity": 1,
                "average_cost": 20,
                "currency": "USD",
                "web_current_value_twd": 700,
            },
            {
                "holding_id": "closed-position",
                "symbol": "CLOSED",
                "market": "TW",
                "asset_type": "FUND",
                "quantity": 0,
                "currency": "TWD",
                "principal_twd": 50000,
                "current_value_twd": 50000,
            },
        ],
    }

    first = store.sync_opening_balance_transactions(
        state,
        "2024-01-29T00:00:00+08:00",
    )
    second = store.sync_opening_balance_transactions(
        state,
        "2024-01-29T00:00:00+08:00",
    )
    ledger = store.ledger_summary()
    performance = store.performance(state)
    transactions = store.list_transactions()

    assert first["active_holding_count"] == 3
    assert first["generated_count"] == 2
    assert first["uncovered_count"] == 1
    assert first["uncovered_symbols"] == ["MISS"]
    assert first["coverage_percent"] == 66.67
    assert second["generated_count"] == 2
    assert ledger["transaction_count"] == 2
    assert ledger["estimated_transaction_count"] == 2
    assert ledger["confirmed_transaction_count"] == 0
    assert ledger["ledger_quality"] == "estimated_opening"
    assert sum(item["amount"] for item in ledger["cashflows"]) == -7500
    assert performance["current_cost"] == 7500
    assert performance["current_value"] == 8800
    assert performance["xirr_percent"] is not None
    assert len(performance["positions"]) == 3
    assert all(item["is_estimated"] for item in transactions)
    assert all(item["currency"] == "TWD" for item in transactions)


def test_opening_ledger_does_not_replace_confirmed_transactions(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    store.add_transaction(
        {
            "side": "BUY",
            "symbol": "AAPL",
            "quantity": 1,
            "price": 100,
            "occurred_at": "2024-01-01T00:00:00Z",
        }
    )

    try:
        store.sync_opening_balance_transactions(
            sample_state(),
            "2024-01-29T00:00:00+08:00",
        )
    except ValueError as error:
        assert "confirmed transactions already exist" in str(error)
    else:
        raise AssertionError("opening ledger replaced confirmed transactions")


def test_history_performance_risk_stress_backtest_and_rebalance(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    state = sample_state()
    assert seed_prices(store) == 300
    store.add_transaction(
        {"side": "BUY", "symbol": "AAPL", "quantity": 10, "price": 100, "occurred_at": "2025-01-01T00:00:00Z"}
    )
    store.add_transaction(
        {"side": "BUY", "symbol": "MSFT", "quantity": 5, "price": 200, "occurred_at": "2025-01-01T00:00:00Z"}
    )

    performance = store.performance(state)
    risk = store.risk(state)
    stress = store.stress_test(state)
    backtest = store.backtest(["AAPL", "MSFT"], strategy="momentum")
    rebalance = store.rebalance(
        state,
        targets={"AAPL": 40, "MSFT": 60},
        cash_reserve_percent=5,
        min_trade_value=1,
    )

    assert performance["status"] == "ready"
    assert performance["current_value"] > performance["current_cost"]
    assert len(performance["positions"]) == 2
    assert risk["status"] == "ready"
    assert risk["sample_count"] >= 90
    assert risk["annualized_volatility_percent"] is not None
    assert risk["var_95_one_day_percent"] is not None
    assert risk["correlations"][0]["sample_count"] >= 90
    assert risk["weight_methodology"] == "current_weights_proxy"
    assert (
        abs(
            sum(
                item["risk_contribution_percent"]
                for item in risk["risk_contributions"]
            )
            - 100
        )
        <= 0.05
    )
    assert (
        risk["risk_contribution_methodology"]
        == "euler_marginal_contribution_from_covariance"
    )
    assert stress["status"] == "ready"
    assert len(stress["scenarios"]) == 4
    assert backtest["ok"] is True
    assert backtest["sample_count"] == 100
    assert backtest["lookahead_protection"]
    assert rebalance["status"] == "draft"
    assert rebalance["execution_policy"] == "simulation_only_human_approval_required"
    assert rebalance["orders"]
    assert all(item["target_weight_percent"] <= 35 for item in rebalance["orders"])
    assert rebalance["cash_reserve_percent"] >= 30


def test_events_persistent_alerts_and_ai_calibration(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    state = sample_state()
    seed_prices(store)
    upcoming = datetime.now(timezone.utc) + timedelta(days=2)
    event = store.add_event(
        {
            "event_type": "earnings",
            "symbol": "AAPL",
            "title": "AAPL earnings",
            "scheduled_at": upcoming.isoformat(),
            "source_url": "https://example.test/private-event",
            "details": {"estimate": 2.1},
        }
    )
    assert event["source_url"].startswith("https://")
    assert event["details"]["estimate"] == 2.1

    first_alerts = store.evaluate_alerts(state)
    second_alerts = store.evaluate_alerts(state)
    assert first_alerts
    assert second_alerts == []
    assert store.acknowledge_alert(first_alerts[0]["alert_event_id"]) is True
    acknowledged = {
        item["alert_event_id"]: item["acknowledged_at"]
        for item in store.list_alert_events()
    }
    assert acknowledged[first_alerts[0]["alert_event_id"]]

    created = datetime.now(timezone.utc) - timedelta(days=31)
    analysis = {
        "generated_at": created.isoformat(),
        "holdings": [
            {
                "symbol": "AAPL",
                "quote": {"price": 100, "provider": "test", "as_of": created.isoformat()},
            }
        ],
        "command_result": {
            "portfolio_score": 75,
            "decision_brief": "Test decision",
            "assessments": [
                {
                    "symbol": "AAPL",
                    "score": 75,
                    "risk_level": "medium",
                    "confidence": {"score": 80},
                    "reasons": ["trend"],
                    "risk_flags": [],
                }
            ],
            "action_plan": [
                {"symbol": "AAPL", "action": "持有並觀察", "title": "Hold AAPL"}
            ],
        },
    }
    assert store.record_decisions(analysis) == 1
    store.add_price_bars(
        [
            {
                "symbol": "AAPL",
                "observed_at": (created + timedelta(days=30, hours=1)).isoformat(),
                "close": 110,
                "provider": "outcome-test",
                "verified": True,
            }
        ]
    )
    assert store.update_decision_outcomes() == 1
    calibration = store.calibration()
    decision = store.decisions()[0]
    assert decision["confidence"] == 0.8
    assert decision["prediction_direction"] == "neutral"
    assert decision["eligible_for_calibration"] == 0
    assert decision["outcome"]["evaluation_status"] == "not_a_directional_forecast"
    assert calibration["status"] == "collecting"
    assert calibration["evaluated_count"] == 0
    assert calibration["not_calibrated_count"] == 1


def test_directional_decision_uses_structured_horizon_threshold_and_probability_scale(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    created = datetime.now(timezone.utc) - timedelta(days=31)
    due_price_at = created + timedelta(days=30, hours=1)
    store.add_price_bars(
        [
            {
                "symbol": "AAPL",
                "observed_at": due_price_at.isoformat(),
                "close": 106,
                "provider": "evaluation-fixture",
                "verified": True,
            }
        ]
    )
    analysis = {
        "generated_at": created.isoformat(),
        "holdings": [
            {
                "symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "quote": {
                    "price": 100,
                    "provider": "decision-fixture",
                    "as_of": created.isoformat(),
                },
            }
        ],
        "command_result": {
            "assessments": [
                {
                    "symbol": "AAPL",
                    "score": 75,
                    "risk_level": "medium",
                    "confidence": {"score": 0.8},
                }
            ],
            "action_plan": [
                {
                    "symbol": "AAPL",
                    "action": "研究用看漲情境",
                    "prediction": {
                        "direction": "bullish",
                        "horizon_days": 30,
                        "return_threshold_percent": 5,
                    },
                }
            ],
        },
    }

    assert store.record_decisions(analysis) == 1
    assert store.update_decision_outcomes() == 1

    decision = store.decisions()[0]
    calibration = store.calibration()
    assert decision["confidence"] == 0.8
    assert decision["prediction_direction"] == "bullish"
    assert decision["horizon_days"] == 30
    assert decision["return_threshold_percent"] == 5
    assert decision["outcome"]["success"] is True
    assert decision["outcome"]["price_observed_at"] == due_price_at.isoformat()
    assert calibration["status"] == "ready"
    assert calibration["evaluated_count"] == 1
    assert calibration["brier_score"] == 0.04
    assert calibration["sample_version"]
    assert any(
        item["dimension"] == "market" and item["key"] == "US"
        for item in calibration["slices"]
    )


def test_v3_confidence_unit_bug_is_migrated_once(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO decisions(
                decision_id, dedupe_key, created_at, symbol, action,
                confidence, score, risk_level, reference_price,
                evidence_encrypted, snapshot_encrypted, user_status,
                outcome_due_at, outcome_encrypted, prediction_direction,
                horizon_days, return_threshold_percent,
                eligible_for_calibration
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', 'pending', ?, '', ?, ?, ?, ?)
            """,
            (
                "legacy-confidence",
                "legacy-confidence-key",
                "2025-01-01T00:00:00+00:00",
                "AAPL",
                "buy",
                0.008,
                75,
                "medium",
                100,
                "2099-01-31T00:00:00+00:00",
                "bullish",
                30,
                0,
                1,
            ),
        )
        connection.execute(
            "DELETE FROM metadata WHERE key='decision_confidence_normalized_v4'"
        )
    store.close()

    migrated = InvestmentAnalyticsStore(tmp_path)
    assert migrated.decisions()[0]["confidence"] == 0.8
    with migrated.connect() as connection:
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key='decision_confidence_normalized_v4'"
            ).fetchone()[0]
            == "complete"
        )
    migrated.close()


def test_buy_and_hold_uses_units_and_allows_weight_drift(tmp_path: Path) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for index in range(35):
        bars.extend(
            [
                {
                    "symbol": "GROW",
                    "observed_at": (start + timedelta(days=index)).isoformat(),
                    "close": 100 + 100 * index / 34,
                    "provider": "drift-fixture",
                    "verified": True,
                },
                {
                    "symbol": "FLAT",
                    "observed_at": (start + timedelta(days=index)).isoformat(),
                    "close": 100,
                    "provider": "drift-fixture",
                    "verified": True,
                },
            ]
        )
    store.add_price_bars(bars)

    result = store.backtest(
        ["GROW", "FLAT"],
        strategy="buy_and_hold",
        initial_capital=1000,
        fee_percent=0,
        slippage_percent=0,
    )

    assert result["ok"] is True
    assert result["methodology"] == "unit_based_holdings_with_natural_weight_drift"
    assert result["ending_value"] == 1500
    assert result["ending_weights"]["GROW"] == 66.6667
    assert result["ending_weights"]["FLAT"] == 33.3333
    assert result["cost_assumptions"]["initial_purchase_included"] is True


def test_public_json_fetch_retries_one_transient_failure() -> None:
    attempts = 0
    waits: list[float] = []

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"ok":true}'

    def opener(_request: Any, *, timeout: float) -> Response:
        nonlocal attempts
        assert timeout == 15
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                "https://example.test/data",
                503,
                "temporary",
                {},
                None,
            )
        return Response()

    result = fetch_json_with_retry(
        "https://example.test/data",
        opener=opener,
        sleep=waits.append,
    )

    assert result == {"ok": True}
    assert attempts == 2
    assert waits == [0.25]


def test_privacy_envelope_round_trip() -> None:
    payload = {"holdings": [{"symbol": "AAPL", "quantity": 2}], "note": "敏感資料"}
    encoded = encode_json_document(payload)

    assert decode_json_document(encoded) == payload
    if sys.platform == "win32":
        assert "windows-dpapi-current-user" in encoded
        assert "敏感資料" not in encoded


def test_runtime_profile_migrates_verified_state_and_owner_lock_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "platform_tools" / "ai-assistant"
    legacy_state = tool_root / "runtime" / "state" / "investment_watch_state.json"
    legacy_state.parent.mkdir(parents=True)
    legacy_state.write_text(
        encode_json_document(
            {
                "portfolio": {"file_name": "legacy.csv", "holding_count": 1},
                "holdings": [{"symbol": "AAPL", "quantity": 1}],
            }
        ),
        encoding="utf-8",
    )
    local_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    monkeypatch.delenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", raising=False)
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "private profile")

    repository = InvestmentWatchRepository(tool_root)

    assert repository.runtime_root == (
        local_data / "GPTBridge" / "ai-assistant" / "private-profile"
    ).resolve()
    assert repository.load_state()["holdings"][0]["symbol"] == "AAPL"
    assert legacy_state.exists()
    assert (repository.state_root / "runtime-migration-manifest.json").exists()
    try:
        InvestmentWatchRepository(tool_root)
    except RuntimeError as error:
        assert "Another process is already active" in str(error)
    else:
        raise AssertionError("a second repository owner must fail closed")
    repository.close()

    lock_path = repository.runtime_root / ".investment-state-owner.lock"
    assert lock_path.exists()
    lock_path.write_text("{stale diagnostic metadata", encoding="utf-8")
    recovered = InvestmentWatchRepository(tool_root)
    assert recovered.load_state()["holdings"][0]["symbol"] == "AAPL"
    recovered.close()
    assert lock_path.exists()


def test_corrupt_state_requires_recovery_instead_of_returning_empty(
    tmp_path: Path,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    repository.save_state(
        {
            "portfolio": {"file_name": "portfolio.csv", "holding_count": 1},
            "holdings": [{"symbol": "AAPL", "quantity": 1}],
        }
    )
    repository.state_path.write_text("{not valid json", encoding="utf-8")

    try:
        repository.load_state()
    except InvestmentStateRecoveryRequired:
        pass
    else:
        raise AssertionError("corrupt investment state must require recovery")

    assert (repository.recovery_root / "latest-recovery-required.json").exists()
    assert list(repository.recovery_root.glob("*.corrupt"))
    repository.close()


def test_legacy_state_upgrade_is_snapshotted_and_atomically_migrated(
    tmp_path: Path,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    repository.state_path.write_text(
        encode_json_document(
            {
                "version": "4.0.0",
                "portfolio": {"file_name": "legacy.xlsx", "holding_count": 1},
                "holdings": [{"symbol": "AAPL", "quantity": 1}],
                "legacy_extension": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )

    migrated = repository.load_state()

    assert migrated["version"] == INVESTMENT_APP_VERSION
    assert migrated["schema_version"] == INVESTMENT_STATE_SCHEMA_VERSION
    assert migrated["legacy_extension"] == {"preserve": True}
    assert migrated["upgrade"]["from_schema"] == 1
    assert migrated["upgrade"]["to_schema"] == INVESTMENT_STATE_SCHEMA_VERSION
    versions = repository.list_state_versions(limit=10)
    assert versions
    assert versions[0]["reason"] == "before_schema_upgrade_v1_to_v2"
    assert migrated["upgrade"]["snapshot_version_id"] == versions[0]["version_id"]
    repository.close()


def test_future_state_schema_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    repository.state_path.write_text(
        encode_json_document(
            {
                "version": "99.0.0",
                "schema_version": INVESTMENT_STATE_SCHEMA_VERSION + 1,
                "portfolio": {"file_name": "future.xlsx", "holding_count": 1},
                "holdings": [{"symbol": "FUTURE", "quantity": 1}],
            }
        ),
        encoding="utf-8",
    )
    original = repository.state_path.read_bytes()

    with pytest.raises(InvestmentStateUpgradeRequired, match="請先升級"):
        repository.load_state()

    assert repository.state_path.read_bytes() == original
    assert not (repository.recovery_root / "latest-recovery-required.json").exists()
    repository.close()


def test_future_analytics_schema_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    with store.connect() as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
    store.close()
    database_path = store.database_path
    original = database_path.read_bytes()

    with pytest.raises(InvestmentAnalyticsUpgradeRequired, match="請先升級"):
        InvestmentAnalyticsStore(tmp_path)

    assert database_path.read_bytes() == original
    assert not (
        store.recovery_root / "latest-database-recovery-required.json"
    ).exists()


def test_service_initialization_failure_releases_repository_owner_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAnalyticsStore:
        def __init__(self, tool_root: Path) -> None:
            del tool_root
            raise RuntimeError("simulated analytics startup failure")

    monkeypatch.setattr(
        investment_watch_module,
        "InvestmentAnalyticsStore",
        FailingAnalyticsStore,
    )
    with pytest.raises(RuntimeError, match="simulated analytics"):
        InvestmentWatchService(tmp_path)

    repository = InvestmentWatchRepository(tmp_path)
    repository.close()


def test_late_service_initialization_failure_releases_all_storage_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingV3:
        def __init__(self, analytics_store: InvestmentAnalyticsStore) -> None:
            del analytics_store
            raise RuntimeError("simulated component startup failure")

    monkeypatch.setattr(investment_watch_module, "InvestmentV3Engine", FailingV3)
    with pytest.raises(RuntimeError, match="simulated component"):
        InvestmentWatchService(tmp_path)

    repository = InvestmentWatchRepository(tmp_path)
    analytics = InvestmentAnalyticsStore(tmp_path)
    analytics.close()
    repository.close()


def test_repository_read_modify_write_methods_hold_one_transaction_lock(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    repository.save_state(sample_state())
    original_load_state = repository.load_state
    ownership_checks: list[bool] = []

    def observed_load_state() -> dict[str, Any]:
        ownership_checks.append(repository._state_lock._is_owned())
        return original_load_state()

    monkeypatch.setattr(repository, "load_state", observed_load_state)
    try:
        repository.replace_holdings(
            [{"symbol": "AAPL", "market": "US", "quantity": 3}],
            change={"action": "concurrency-test"},
        )
        repository.save_local_ai_result(
            {"status": "ready"},
            {"headline": "serial"},
            [],
            {"decision_brief": "hold"},
        )
        repository.save_mobile_sync_remote_url("https://sync.example.test")
        run = repository.add_ai_run(
            "local_risk_monitor",
            "local-risk-ai",
            "run",
            "running",
        )
        repository.update_ai_run(run["run_id"], status="completed", content="ok")
        repository.add_ai_run(
            "local_risk_monitor",
            "local-risk-ai",
            "recover",
            "running",
        )
        repository.recover_interrupted_ai_runs()
    finally:
        repository.close()

    assert ownership_checks
    assert all(ownership_checks)


def test_repository_concurrent_rmw_preserves_both_writers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    repository.save_state(sample_state())
    original_save_state = repository.save_state
    first_waiting_to_save = threading.Event()
    release_first_writer = threading.Event()
    second_finished = threading.Event()
    failures: list[BaseException] = []

    def delayed_save_state(state: dict[str, Any]) -> dict[str, Any]:
        if (
            threading.current_thread().name == "holdings-writer"
            and not first_waiting_to_save.is_set()
        ):
            first_waiting_to_save.set()
            if not release_first_writer.wait(timeout=5):
                raise TimeoutError("first repository writer was not released")
        return original_save_state(state)

    def replace_holdings() -> None:
        try:
            repository.replace_holdings(
                [{"symbol": "AAPL", "market": "US", "quantity": 7}],
                change={"action": "concurrent-holdings"},
            )
        except BaseException as error:
            failures.append(error)

    def save_remote_url() -> None:
        try:
            repository.save_mobile_sync_remote_url("https://sync.example.test")
        except BaseException as error:
            failures.append(error)
        finally:
            second_finished.set()

    monkeypatch.setattr(repository, "save_state", delayed_save_state)
    first = threading.Thread(target=replace_holdings, name="holdings-writer")
    second = threading.Thread(target=save_remote_url, name="settings-writer")
    try:
        first.start()
        assert first_waiting_to_save.wait(timeout=5)
        second.start()
        second_completed_while_first_paused = second_finished.wait(timeout=0.2)
        release_first_writer.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive()
        assert not second.is_alive()
        assert not second_completed_while_first_paused
        assert not failures

        state = repository.load_state()
        assert state["holdings"][0]["quantity"] == 7
        assert (
            state["mobile_sync_settings"]["remote_base_url"]
            == "https://sync.example.test"
        )
    finally:
        release_first_writer.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
        repository.close()


def test_analytics_owner_lock_rejects_second_instance_and_ignores_stale_metadata(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    try:
        InvestmentAnalyticsStore(tmp_path)
    except RuntimeError as error:
        assert "Another process is already active" in str(error)
    else:
        raise AssertionError("a second analytics writer must fail closed")
    store.close()

    lock_path = tmp_path / "runtime" / ".investment-analytics-owner.lock"
    assert lock_path.exists()
    lock_path.write_text("{stale diagnostic metadata", encoding="utf-8")
    recovered = InvestmentAnalyticsStore(tmp_path)
    assert recovered.database_security_status()["database_path"]
    recovered.close()
    assert lock_path.exists()


@pytest.mark.parametrize(
    ("module_name", "component"),
    [
        ("ai_nexus.investment_repository", "repository-owner-test"),
        ("ai_nexus.investment_analytics", "analytics-owner-test"),
    ],
)
def test_runtime_owner_lock_is_exclusive_across_processes(
    tmp_path: Path,
    module_name: str,
    component: str,
) -> None:
    lock_path = tmp_path / f"{component}.lock"
    script = """
import importlib
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
module = importlib.import_module(sys.argv[2])
lock = module._RuntimeOwnerLock(Path(sys.argv[3]), sys.argv[4])
lock.acquire()
print("ready", flush=True)
sys.stdin.readline()
lock.release()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(SERVICES_ROOT),
            module_name,
            str(lock_path),
            component,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    module = (
        investment_repository_module
        if module_name.endswith("investment_repository")
        else investment_analytics_module
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        contender = module._RuntimeOwnerLock(lock_path, component)
        with pytest.raises(RuntimeError, match="Another process is already active"):
            contender.acquire()
    finally:
        output, errors = process.communicate(input="\n", timeout=10)
    assert process.returncode == 0, f"{output}\n{errors}"
    assert lock_path.exists()

    successor = module._RuntimeOwnerLock(lock_path, component)
    successor.acquire()
    successor.release()
    assert lock_path.exists()


def test_repository_ai_run_history_is_append_only(tmp_path: Path) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    try:
        created = [
            repository.add_ai_run(
                "local_risk_monitor",
                "local-risk-ai",
                f"run-{index}",
                "completed",
                content=f"result-{index}",
            )
            for index in range(35)
        ]
        persisted = repository.load_state()["ai_runs"]
    finally:
        repository.close()

    assert len(persisted) == 35
    assert {item["run_id"] for item in persisted} == {
        item["run_id"] for item in created
    }


def test_repository_rejects_traversing_configured_data_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    traversing_root = configured / ".." / "escaped-data"
    monkeypatch.setenv(
        "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT",
        str(traversing_root),
    )

    with pytest.raises(RuntimeError, match="must not contain traversal"):
        InvestmentWatchRepository(tool_root)

    assert not (tmp_path / "escaped-data").exists()


def test_repository_rejects_linked_data_root_ancestor(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-data"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(linked_root))

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        InvestmentWatchRepository(tool_root)

    assert list(outside.iterdir()) == []


def test_repository_rejects_simulated_reparse_data_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    original_check = investment_repository_module._is_link_or_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == configured or original_check(Path(path))

    monkeypatch.setattr(
        investment_repository_module,
        "_is_link_or_reparse",
        simulated_reparse,
    )
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(configured))

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        InvestmentWatchRepository(tool_root)

    assert not (configured / "default").exists()


def test_analytics_rejects_traversing_configured_data_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setenv(
        "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT",
        str(configured / ".." / "escaped-analytics"),
    )

    with pytest.raises(RuntimeError, match="must not contain traversal"):
        InvestmentAnalyticsStore(tool_root)

    assert not (tmp_path / "escaped-analytics").exists()


def test_legacy_migration_rejects_linked_state_tree_without_touching_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "platform_tools" / "ai-assistant"
    legacy_runtime = tool_root / "runtime"
    legacy_runtime.mkdir(parents=True)
    outside_state = tmp_path / "outside-legacy-state"
    outside_state.mkdir()
    source = outside_state / "investment_watch_state.json"
    source.write_text("preserve exactly", encoding="utf-8")
    legacy_state = legacy_runtime / "state"
    try:
        legacy_state.symlink_to(outside_state, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    local_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    monkeypatch.delenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        InvestmentWatchRepository(tool_root)

    assert source.read_text(encoding="utf-8") == "preserve exactly"
    assert legacy_state.is_symlink()


def test_legacy_migration_rejects_simulated_reparse_tree(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "platform_tools" / "ai-assistant"
    legacy_state = tool_root / "runtime" / "state"
    legacy_state.mkdir(parents=True)
    source = legacy_state / "investment_watch_state.json"
    source.write_text("preserve exactly", encoding="utf-8")
    local_data = tmp_path / "local-app-data"
    original_check = investment_repository_module._is_link_or_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == legacy_state or original_check(Path(path))

    monkeypatch.setattr(
        investment_repository_module,
        "_is_link_or_reparse",
        simulated_reparse,
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    monkeypatch.delenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        InvestmentWatchRepository(tool_root)

    assert source.read_text(encoding="utf-8") == "preserve exactly"


def test_analytics_legacy_migration_rejects_simulated_reparse_tree(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    tool_root = tmp_path / "platform_tools" / "ai-assistant"
    legacy_backups = tool_root / "runtime" / "investment_backups"
    legacy_backups.mkdir(parents=True)
    source = legacy_backups / "manual.ivault"
    source.write_bytes(b"preserve exactly")
    local_data = tmp_path / "local-app-data"
    original_check = investment_repository_module._is_link_or_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == legacy_backups or original_check(Path(path))

    monkeypatch.setattr(
        investment_repository_module,
        "_is_link_or_reparse",
        simulated_reparse,
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    monkeypatch.delenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        InvestmentAnalyticsStore(tool_root)

    assert source.read_bytes() == b"preserve exactly"


def test_service_seeds_opening_ledger_and_returns_refreshed_state(
    tmp_path: Path,
) -> None:
    service = InvestmentWatchService(tmp_path)
    source = tmp_path / "portfolio.xlsx"
    source.write_bytes(b"test source")
    service.repository.save_portfolio(
        source,
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 2,
                "average_cost": 100,
                "currency": "USD",
                "principal_twd": 6500,
                "web_current_value_twd": 7000,
            }
        ],
    )

    async def exercise() -> None:
        event, result = await service.handle(
            "investment_watch_seed_opening_ledger",
            {"occurred_at": "2024-01-29T00:00:00+08:00"},
        )
        assert event == "investment_watch_seed_opening_ledger_result"
        assert result["ok"] is True
        assert result["opening_ledger"]["generated_count"] == 1
        assert Path(result["safety_backup"]["path"]).exists()
        assert result["state"]["analytics"]["ledger"]["ledger_quality"] == "estimated_opening"
        assert "尚未建立交易帳本" not in "".join(
            result["state"]["analytics"]["data_health"]["warnings"]
        )

    asyncio.run(exercise())


def test_principal_basis_uses_twd_principal_and_asset_fx(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    service.v3.add_fx_rates(
        [
            {
                "base_currency": "USD",
                "quote_currency": "TWD",
                "observed_at": "2026-07-22T00:00:00Z",
                "rate": 32,
                "provider": "huanan-bank-spot-mid",
                "verified": True,
            }
        ]
    )

    enriched = service._enrich_holding_principal_basis(
        {
            "symbol": "AAPL",
            "quantity": 2,
            "currency": "USD",
            "principal_amount": 3200,
            "principal_currency": "TWD",
            "principal_twd": 3200,
        }
    )

    assert enriched["principal_twd"] == 3200
    assert enriched["average_cost"] == 50
    assert enriched["average_cost_currency"] == "USD"
    assert enriched["average_cost_fx_rate"] == 32


def test_reimport_preserves_matching_online_enrichment(tmp_path: Path) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    source = tmp_path / "portfolio.xlsx"
    source.write_bytes(b"source")
    first = repository.save_portfolio(
        source,
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "source_row": 10,
                "quantity": 1,
                "principal_twd": 3000,
            }
        ],
    )
    holding = first["holdings"][0]
    holding["web_current_value_twd"] = 3200
    holding["dividend_frequency"] = "quarterly"
    holding["average_cost"] = 95
    holding["average_cost_method"] = "principal_twd_huanan_current_fx_estimate"
    repository.save_state(first)

    second = repository.save_portfolio(
        source,
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "source_row": 10,
                "quantity": 2,
                "principal_twd": 6000,
                "average_cost": None,
            }
        ],
    )
    reimported = second["holdings"][0]

    assert reimported["holding_id"] == holding["holding_id"]
    assert reimported["quantity"] == 2
    assert reimported["principal_twd"] == 6000
    assert reimported["web_current_value_twd"] == 3200
    assert reimported["dividend_frequency"] == "quarterly"
    assert reimported["average_cost"] == 95


def test_retrying_same_import_fingerprint_preserves_manual_changes(
    tmp_path: Path,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    source = tmp_path / "portfolio.xlsx"
    source.write_bytes(b"same immutable import")
    first = repository.save_portfolio(
        source,
        [{"symbol": "AAPL", "market": "US", "quantity": 1}],
        import_fingerprint="stable-import-fingerprint",
    )
    manually_changed = repository.replace_holdings(
        [{**first["holdings"][0], "quantity": 7}],
        change={"action": "manual_quantity_correction"},
    )
    versions_before_retry = repository.list_state_versions(limit=100)

    retried = repository.save_portfolio(
        source,
        [{"symbol": "AAPL", "market": "US", "quantity": 1}],
        import_fingerprint="stable-import-fingerprint",
    )

    assert retried["holdings"][0]["quantity"] == 7
    assert retried["portfolio"]["manual_revision"] == 1
    assert repository.list_state_versions(limit=100) == versions_before_retry
    assert manually_changed["updated_at"] == retried["updated_at"]
    repository.close()


def test_state_versions_are_never_pruned_automatically(
    tmp_path: Path,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    state = {
        "portfolio": {"file_name": "portfolio.xlsx", "holding_count": 1},
        "holdings": [{"symbol": "AAPL", "quantity": 1}],
    }
    for index in range(35):
        repository.create_state_version(
            {**state, "sequence": index},
            reason=f"retention-{index}",
        )

    assert len(list(repository.history_root.glob("*.json"))) == 35
    assert len(repository.list_state_versions(limit=100)) == 35
    repository.close()


def test_startup_schema_migration_creates_prechange_backup(
    tmp_path: Path,
) -> None:
    original = InvestmentAnalyticsStore(tmp_path)
    original.add_transaction(
        {
            "transaction_id": "schema-migration-proof",
            "side": "BUY",
            "symbol": "AAPL",
            "quantity": 1,
            "price": 100,
        }
    )
    with original.connect() as connection:
        connection.execute(
            "UPDATE metadata SET value='3' WHERE key='schema_version'"
        )
    original.close()

    migrated = InvestmentAnalyticsStore(tmp_path)
    migration_backups = [
        item
        for item in migrated.list_backups()
        if item["label"] == "pre-schema-migration"
    ]

    assert migration_backups
    assert all(
        item["integrity_verified"] is True for item in migration_backups
    )
    assert any(
        item["transaction_id"] == "schema-migration-proof"
        for item in migrated.list_transactions()
    )
    migrated.close()


def test_failed_restore_rolls_back_database_and_portfolio_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    store = InvestmentAnalyticsStore(tmp_path)
    repository.save_state(
        {
            "portfolio": {"file_name": "old.xlsx", "holding_count": 1},
            "holdings": [{"symbol": "OLD", "quantity": 1}],
        }
    )
    store.add_transaction(
        {
            "transaction_id": "old-transaction",
            "side": "BUY",
            "symbol": "OLD",
            "quantity": 1,
            "price": 10,
        }
    )
    backup = store.backup_database("restore-source")

    repository.save_state(
        {
            "portfolio": {"file_name": "current.xlsx", "holding_count": 1},
            "holdings": [{"symbol": "CURRENT", "quantity": 2}],
        }
    )
    store.add_transaction(
        {
            "transaction_id": "current-transaction",
            "side": "BUY",
            "symbol": "CURRENT",
            "quantity": 2,
            "price": 20,
        }
    )
    current_state_payload = repository.state_path.read_bytes()
    manifest = json.loads(
        Path(str(backup["manifest_path"])).read_text(encoding="utf-8")
    )
    incoming_state_name = next(
        item["name"]
        for item in manifest["files"]
        if item["role"] == "portfolio_state"
    )
    incoming_state_payload = (
        store.backup_root / incoming_state_name
    ).read_bytes()
    original_atomic_write = investment_analytics_module._atomic_write_bytes
    failed_once = False

    def fail_incoming_state_write(path: Path, payload: bytes) -> None:
        nonlocal failed_once
        if (
            not failed_once
            and Path(path) == repository.state_path
            and payload == incoming_state_payload
        ):
            failed_once = True
            raise OSError("synthetic state publication failure")
        original_atomic_write(Path(path), payload)

    monkeypatch.setattr(
        investment_analytics_module,
        "_atomic_write_bytes",
        fail_incoming_state_write,
    )
    with pytest.raises(OSError, match="synthetic state publication failure"):
        store.restore_database(Path(str(backup["path"])).name)

    assert failed_once is True
    assert repository.state_path.read_bytes() == current_state_payload
    transaction_ids = {
        item["transaction_id"] for item in store.list_transactions()
    }
    assert transaction_ids == {"old-transaction", "current-transaction"}
    monkeypatch.setattr(
        investment_analytics_module,
        "_atomic_write_bytes",
        original_atomic_write,
    )
    store.close()
    repository.close()


def test_failed_restore_preserves_new_state_when_original_was_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    store = InvestmentAnalyticsStore(tmp_path)
    repository.save_state(
        {
            "portfolio": {"file_name": "backup.xlsx", "holding_count": 1},
            "holdings": [{"symbol": "BACKUP", "quantity": 1}],
        }
    )
    backup = store.backup_database("state-source")
    manifest = json.loads(
        Path(str(backup["manifest_path"])).read_text(encoding="utf-8")
    )
    incoming_state_name = next(
        item["name"]
        for item in manifest["files"]
        if item["role"] == "portfolio_state"
    )
    incoming_state_payload = (
        store.backup_root / incoming_state_name
    ).read_bytes()
    original_state_archive = tmp_path / "original-state-preserved.vault"
    repository.state_path.replace(original_state_archive)
    original_atomic_write = investment_analytics_module._atomic_write_bytes
    failed_once = False

    def publish_then_fail(path: Path, payload: bytes) -> None:
        nonlocal failed_once
        original_atomic_write(Path(path), payload)
        if (
            not failed_once
            and Path(path) == repository.state_path
            and payload == incoming_state_payload
        ):
            failed_once = True
            raise OSError("synthetic post-publication failure")

    monkeypatch.setattr(
        investment_analytics_module,
        "_atomic_write_bytes",
        publish_then_fail,
    )
    with pytest.raises(OSError, match="synthetic post-publication failure"):
        store.restore_database(Path(str(backup["path"])).name)

    assert failed_once is True
    assert repository.state_path.exists() is False
    retained = list(
        (store.recovery_root / "failed-restore-state").glob("*.statevault")
    )
    assert len(retained) == 1
    assert retained[0].read_bytes() == incoming_state_payload
    assert original_state_archive.exists()
    monkeypatch.setattr(
        investment_analytics_module,
        "_atomic_write_bytes",
        original_atomic_write,
    )
    store.close()
    repository.close()


def test_incomplete_backup_is_preserved_outside_published_backup_list(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    state_path = (
        store.runtime_root / "state" / "investment_watch_state.json"
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{corrupt protected state", encoding="utf-8")

    with pytest.raises(ValueError):
        store.backup_database("must-fail")

    assert store.list_backups() == []
    recovery_sets = list(
        (store.recovery_root / "incomplete-backups").glob("*")
    )
    assert len(recovery_sets) == 1
    assert list(recovery_sets[0].glob("*.ivault"))
    assert (recovery_sets[0] / "recovery.json").exists()
    store.close()


def test_backup_retention_keeps_only_latest_generation(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    backups = [store.backup_database("automatic") for _ in range(6)]

    result = store.prune_backups(max_total=5, max_automatic=2)

    assert result["removed"] == 5
    assert result["retained"] == 1
    assert result["over_total_limit"] == 0
    assert result["over_automatic_limit"] == 0
    assert sum(Path(str(item["path"])).exists() for item in backups) == 1
    store.close()


def test_cancelled_excel_import_returns_promptly_while_durable_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = InvestmentWatchService(tmp_path)
    source = tmp_path / "cancelled-request.xlsx"
    source.write_bytes(b"durable excel request")
    started = threading.Event()
    finished = threading.Event()

    def durable_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        time.sleep(0.25)
        service.repository.save_state(
            {
                "portfolio": {
                    "file_name": "cancelled-request.xlsx",
                    "holding_count": 1,
                },
                "holdings": [{"symbol": "AAPL", "quantity": 1}],
            }
        )
        finished.set()
        return {
            "ok": True,
            "state": service.repository.load_state(),
            "import_fingerprint": "a" * 64,
            "imported_row_count": 1,
        }

    monkeypatch.setattr(
        service,
        "_import_excel_mapping_sync",
        durable_worker,
    )

    async def exercise() -> None:
        operation = asyncio.create_task(
            service._import_excel_mapping(
                {
                    "path": str(source),
                    "refresh_quotes": False,
                }
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        cancelled_at = time.monotonic()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert time.monotonic() - cancelled_at < 0.1
        assert finished.is_set() is False
        assert await asyncio.to_thread(finished.wait, 2.0) is True
        journal = service.analytics_store.resumable_import_operations()
        for _attempt in range(100):
            if not journal:
                break
            await asyncio.sleep(0.01)
            journal = service.analytics_store.resumable_import_operations()
        assert journal == []
        await service.shutdown()

    asyncio.run(exercise())

    assert finished.is_set() is True
    assert service.repository.load_state()["holdings"][0]["symbol"] == "AAPL"


def test_excel_import_timeout_is_pollable_and_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = InvestmentWatchService(tmp_path)
    service.IMPORT_INLINE_WAIT_SECONDS = 0.01
    source = tmp_path / "slow-import.xlsx"
    source.write_bytes(b"slow durable excel request")
    commit_count = 0

    def durable_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal commit_count
        time.sleep(0.1)
        commit_count += 1
        service.repository.save_state(
            {
                "portfolio": {
                    "file_name": source.name,
                    "holding_count": 1,
                    "import_fingerprint": "b" * 64,
                },
                "holdings": [{"symbol": "MSFT", "quantity": 2}],
            }
        )
        return {
            "ok": True,
            "message": "completed",
            "state": service.repository.load_state(),
            "import_mode": "snapshot_manual_mapping",
            "import_fingerprint": "b" * 64,
            "imported_row_count": 1,
            "skipped_row_count": 0,
        }

    monkeypatch.setattr(service, "_import_excel_mapping_sync", durable_worker)

    async def exercise() -> None:
        first = await service._import_excel_mapping(
            {"path": str(source), "refresh_quotes": False}
        )
        assert first["ok"] is True
        assert first["processing"] is True
        assert first["operation_status"] in {"queued", "processing"}
        operation_id = str(first["operation_id"])
        assert len(str(first["fingerprint"])) == 64

        final: dict[str, Any] = first
        for _attempt in range(100):
            final = await service._import_excel_mapping(
                {"operation_id": operation_id, "poll": True}
            )
            if final.get("processing") is False:
                break
            await asyncio.sleep(0.01)
        assert final["ok"] is True
        assert final["operation_status"] == "completed"
        assert final["import_fingerprint"] == "b" * 64
        assert final["state"]["holdings"][0]["symbol"] == "MSFT"

        retried = await service._import_excel_mapping(
            {"path": str(source), "refresh_quotes": False}
        )
        assert retried["operation_id"] == operation_id
        assert retried["processing"] is False
        assert commit_count == 1
        operation = service.analytics_store.get_import_operation(operation_id)
        assert operation is not None
        assert operation["status"] == "completed"
        assert operation["attempt_count"] == 1
        assert [item["status"] for item in operation["history"]][:2] == [
            "queued",
            "processing",
        ]
        await service.shutdown()

    asyncio.run(exercise())


def test_interrupted_excel_import_resumes_after_service_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "restart-import.xlsx"
    source.write_bytes(b"restartable excel request")
    first = InvestmentWatchService(tmp_path)
    operation = first.analytics_store.create_or_resume_import_operation(
        "c" * 64,
        {"path": str(source), "refresh_quotes": False},
    )
    first.analytics_store.update_import_operation(
        str(operation["operation_id"]),
        status="processing",
        reason="simulated_process_exit",
        increment_attempt=True,
    )
    first.analytics_store.close()
    first.repository.close()

    resumed = InvestmentWatchService(tmp_path)
    commit_count = 0

    def durable_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal commit_count
        commit_count += 1
        resumed.repository.save_state(
            {
                "portfolio": {
                    "file_name": source.name,
                    "holding_count": 1,
                },
                "holdings": [{"symbol": "NVDA", "quantity": 3}],
            }
        )
        return {
            "ok": True,
            "state": resumed.repository.load_state(),
            "import_fingerprint": "d" * 64,
            "imported_row_count": 1,
        }

    monkeypatch.setattr(resumed, "_import_excel_mapping_sync", durable_worker)

    async def exercise() -> None:
        await resumed.start()
        operation_id = str(operation["operation_id"])
        for _attempt in range(100):
            current = resumed.analytics_store.get_import_operation(operation_id)
            assert current is not None
            if current["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        current = resumed.analytics_store.get_import_operation(operation_id)
        assert current is not None
        assert current["status"] == "completed"
        assert current["attempt_count"] == 2
        assert commit_count == 1
        assert resumed.repository.load_state()["holdings"][0]["symbol"] == "NVDA"
        await resumed.shutdown()

    asyncio.run(exercise())


def test_excel_import_shutdown_drain_is_bounded_and_preserves_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = InvestmentWatchService(tmp_path)
    service.IMPORT_INLINE_WAIT_SECONDS = 0.01
    service.IMPORT_SHUTDOWN_DRAIN_SECONDS = 0.02
    source = tmp_path / "shutdown-import.xlsx"
    source.write_bytes(b"bounded shutdown request")
    started = threading.Event()
    finished = threading.Event()

    def slow_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        assert threading.current_thread().daemon is True
        started.set()
        time.sleep(0.25)
        service.repository.save_state(
            {
                "portfolio": {
                    "file_name": source.name,
                    "holding_count": 1,
                },
                "holdings": [{"symbol": "AMD", "quantity": 4}],
            }
        )
        finished.set()
        return {
            "ok": True,
            "state": service.repository.load_state(),
            "import_fingerprint": "e" * 64,
            "imported_row_count": 1,
        }

    monkeypatch.setattr(service, "_import_excel_mapping_sync", slow_worker)

    async def exercise() -> None:
        response = await service._import_excel_mapping(
            {"path": str(source), "refresh_quotes": False}
        )
        assert response["processing"] is True
        assert started.is_set() is True
        operation_id = str(response["operation_id"])

        shutdown_started = time.monotonic()
        await service.shutdown()
        assert time.monotonic() - shutdown_started < 0.15
        operation = service.analytics_store.get_import_operation(operation_id)
        assert operation is not None
        assert operation["status"] == "resume_pending"
        assert finished.is_set() is False
        assert await asyncio.to_thread(finished.wait, 2.0) is True
        for _attempt in range(100):
            if not service._import_operation_tasks:
                break
            await asyncio.sleep(0.01)
        assert service.repository.load_state()["holdings"][0]["symbol"] == "AMD"

    asyncio.run(exercise())


def test_generic_durable_worker_cancellation_never_waits_for_thread(
    tmp_path: Path,
) -> None:
    service = InvestmentWatchService(tmp_path)
    started = threading.Event()
    finished = threading.Event()

    def slow_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        assert threading.current_thread().daemon is True
        started.set()
        time.sleep(0.25)
        service.repository.save_state(
            {
                "portfolio": {
                    "file_name": "generic-worker.xlsx",
                    "holding_count": 1,
                },
                "holdings": [{"symbol": "TSM", "quantity": 1}],
            }
        )
        finished.set()
        return {"ok": True}

    async def exercise() -> None:
        operation = asyncio.create_task(
            service._run_durable_worker(slow_worker, {})
        )
        while not started.is_set():
            await asyncio.sleep(0.005)
        cancelled_at = time.monotonic()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert time.monotonic() - cancelled_at < 0.1
        assert finished.is_set() is False
        assert await asyncio.to_thread(finished.wait, 2.0) is True
        for _attempt in range(100):
            if not service._durable_worker_tasks:
                break
            await asyncio.sleep(0.01)
        assert service.repository.load_state()["holdings"][0]["symbol"] == "TSM"
        await service.shutdown()

    asyncio.run(exercise())


def test_transaction_delete_is_tombstone_with_audit_history(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    transaction = store.add_transaction(
        {
            "transaction_id": "keep-history",
            "side": "BUY",
            "symbol": "AAPL",
            "quantity": 1,
            "price": 100,
            "note": "must remain recoverable",
        }
    )

    assert store.delete_transaction(transaction["transaction_id"]) is True
    assert store.list_transactions() == []
    retained = store.list_transactions(include_deleted=True)
    assert len(retained) == 1
    assert retained[0]["transaction_id"] == "keep-history"
    assert retained[0]["deleted"] is True
    assert retained[0]["delete_reason"] == "user_requested"
    assert retained[0]["note"] == "must remain recoverable"
    audit = store.list_audit_log(20)
    event = next(
        item for item in audit if item["action"] == "transaction_tombstoned"
    )
    assert event["details"]["transaction_id"] == "keep-history"
    assert event["details"]["physical_delete"] is False
    store.close()


def test_scheduler_history_uses_logical_archive_without_delete(
    tmp_path: Path,
) -> None:
    store = InvestmentAnalyticsStore(tmp_path)
    notifications = NotificationManager(store)

    async def cycle() -> dict[str, Any]:
        return {"ok": True}

    automation = InvestmentAutomation(store, notifications, cycle)
    with store.connect() as connection:
        connection.executemany(
            """
            INSERT INTO scheduler_runs(
                run_id, job_name, started_at, finished_at, status,
                detail_encrypted
            ) VALUES(?, 'test', ?, ?, 'completed', '')
            """,
            [
                (
                    f"run-{index:03d}",
                    f"2026-01-01T00:{index:02d}:00+00:00",
                    f"2026-01-01T00:{index:02d}:01+00:00",
                )
                for index in range(25)
            ],
        )
    store.set_setting("scheduler_run_retention_count", 20)

    assert automation._prune_scheduler_runs() == 0
    with store.connect() as connection:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM scheduler_runs"
            ).fetchone()[0]
        )
    storage = store.get_setting("scheduler_run_storage", {})
    assert count == 25
    assert storage["archive_count"] == 5
    assert storage["automatic_delete"] is False
    store.close()


def test_snapshot_coordinator_always_locks_repository_before_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = InvestmentWatchService(tmp_path)
    order: list[str] = []
    repository_gate = service.repository.exclusive_data_access
    database_gate = service.analytics_store.exclusive_data_access

    @contextmanager
    def traced_repository() -> Any:
        order.append("repository-enter")
        with repository_gate():
            yield
        order.append("repository-exit")

    @contextmanager
    def traced_database() -> Any:
        order.append("database-enter")
        with database_gate():
            yield
        order.append("database-exit")

    monkeypatch.setattr(
        service.repository,
        "exclusive_data_access",
        traced_repository,
    )
    monkeypatch.setattr(
        service.analytics_store,
        "exclusive_data_access",
        traced_database,
    )

    with service._snapshot_coordinator() as state:
        assert isinstance(state, dict)
        order.append("snapshot")

    assert order == [
        "repository-enter",
        "database-enter",
        "snapshot",
        "database-exit",
        "repository-exit",
    ]
    asyncio.run(service.shutdown())


def test_local_ai_error_rotation_preserves_every_archive(
    tmp_path: Path,
) -> None:
    service = InvestmentWatchService(tmp_path)
    error_path = service.repository.runtime_root / "local-ai-errors.jsonl"
    original = b"x" * (2 * 1024 * 1024)
    error_path.write_bytes(original)

    service._record_local_ai_error(
        "investment_watch_get_state",
        {},
        RuntimeError("synthetic retained error"),
    )

    archives = list(
        (
            service.repository.runtime_root / "local-ai-error-archives"
        ).glob("*.jsonl")
    )
    assert len(archives) == 1
    assert archives[0].read_bytes() == original
    assert error_path.exists()
    assert b"synthetic retained error" not in error_path.read_bytes()
    asyncio.run(service.shutdown())


def test_service_exposes_all_v2_commands(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    commands = {
        "investment_watch_get_analytics",
        "investment_watch_add_transaction",
        "investment_watch_seed_opening_ledger",
        "investment_watch_delete_transaction",
        "investment_watch_import_price_history",
        "investment_watch_sync_intelligence",
        "investment_watch_run_stress_test",
        "investment_watch_run_backtest",
        "investment_watch_plan_rebalance",
        "investment_watch_add_event",
        "investment_watch_add_alert_rule",
        "investment_watch_acknowledge_alert",
        "investment_watch_set_decision_status",
        "investment_watch_update_v2_settings",
        "investment_watch_revoke_mobile_sync_pairing",
    }

    assert all(service.owns(command) for command in commands)

    async def exercise() -> None:
        _event, transaction = await service.handle(
            "investment_watch_add_transaction",
            {"side": "BUY", "symbol": "AAPL", "quantity": 1, "price": 100},
        )
        assert transaction["ok"] is True
        _event, settings = await service.handle(
            "investment_watch_update_v2_settings",
            {"base_currency": "USD", "mobile_pairing_ttl_hours": 12},
        )
        assert settings["settings"]["base_currency"] == "USD"
        _event, revoked = await service.handle(
            "investment_watch_revoke_mobile_sync_pairing",
            {},
        )
        assert revoked["mobile_sync"]["pairing_revoked"] is True

    asyncio.run(exercise())
