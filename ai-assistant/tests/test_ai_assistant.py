from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from ai_nexus.infrastructure.privacy import encode_binary_document
from ai_nexus.integration.star_channel import InvestmentAiConnections
from ai_nexus.application.investment_watch import InvestmentWatchService
SYSTEM_RESCUE_SERVICES = (
    Path(__file__).resolve().parents[2]
    / "main-system"
    / "src"
    / "backend"
    / "services"
)
if str(SYSTEM_RESCUE_SERVICES) not in sys.path:
    sys.path.insert(0, str(SYSTEM_RESCUE_SERVICES))
from tasks.central_repair import database_integrity


TOOL_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_test_targets_exist() -> None:
    manifest = json.loads((TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    missing = [target for target in manifest["test_targets"] if not (TOOL_ROOT / target).is_file()]
    assert missing == []


def test_investment_conversation_is_separate_and_uses_automatic_models() -> None:
    manifest = json.loads((TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    policy = manifest["capabilities"]["investment-manager"]

    assert policy["conversation_entry"] == "ai-assistant-window-only"
    assert policy["conversation_isolated_from"] == "star-chat"
    assert policy["model_selection"] == "automatic-only-no-manual-picker"
    assert policy["primary_business_models"] == [
        "ibm/granite4.2:30b-q4_K_M",
    ]
    assert policy["market_search_owner"] == "star-main-native-model"
    assert policy["market_search_owner_label"] == "星澄原生模型"
    assert policy["realtime_information_search_owner"] == "star-main-native-model"
    assert policy["realtime_information_search_owner_label"] == "星澄原生模型"
    assert policy["realtime_information_search_scope"] == [
        "dividends",
        "prices",
        "net-asset-values",
        "other-current-market-information",
    ]


def test_clear_state_requires_explicit_confirmation(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "test")
    service = InvestmentWatchService(TOOL_ROOT)
    try:
        _event, rejected = asyncio.run(service.handle("investment_watch_clear_state", {}))
        assert rejected["ok"] is False
        assert "明確確認" in rejected["message"]

        _event, accepted = asyncio.run(
            service.handle("investment_watch_clear_state", {"confirmed": True})
        )
        assert accepted["ok"] is True
    finally:
        service._close_storage()


def test_permanent_clear_removes_business_state_and_recovery_copies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "test")
    service = InvestmentWatchService(TOOL_ROOT)
    try:
        service.analytics_store.set_setting("obsolete", {"value": True})
        service.analytics_store.backup_database("obsolete")
        service.repository.create_state_version(
            service.repository.load_state(),
            reason="obsolete",
        )

        _event, result = asyncio.run(
            service.handle(
                "investment_watch_clear_state",
                {"confirmed": True, "permanent": True},
            )
        )

        assert result["ok"] is True
        assert result["permanent"] is True
        assert result["safety_backup"]["created"] is False
        assert service.analytics_store.get_setting("obsolete") is None
        for root in (
            service.analytics_store.backup_root,
            service.analytics_store.recovery_root,
            service.repository.history_root,
            service.repository.recovery_root,
        ):
            assert not root.exists() or list(root.iterdir()) == []
    finally:
        service._close_storage()


def test_auto_repair_defers_encrypted_database_validation_to_owner(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE health_check(value TEXT NOT NULL)")
        connection.execute("INSERT INTO health_check(value) VALUES ('ok')")
        connection.commit()
        database_bytes = connection.serialize()
    finally:
        connection.close()

    database = tmp_path / "investment_analytics_v2.sqlite3"
    database.write_bytes(
        encode_binary_document(
            database_bytes,
            purpose="investment-analytics-database-v3",
        )
    )
    with pytest.raises(
        sqlite3.DatabaseError,
        match="protected database verification belongs to the owner tool",
    ):
        database_integrity(database, "ai-assistant")


def test_manual_dividend_frequency_is_editable_and_updates_estimates() -> None:
    holding = InvestmentWatchService._normalized_manual_holding(
        {
            "symbol": "2330",
            "market": "TW",
            "asset_type": "STOCK",
            "currency": "TWD",
            "quantity": 10,
            "average_cost": 100,
            "dividend_per_unit": 3,
            "dividend_frequency": "quarterly",
        }
    )

    assert holding["dividend_frequency"] == "quarterly"
    assert holding["dividend_frequency_label"] == "每季"
    assert holding["dividend_frequency_per_year"] == 4
    assert holding["dividend_frequency_source"] == "manual"
    assert holding["estimated_annual_dividend_twd"] == 120
    assert holding["estimated_weekly_dividend_twd"] == 120 / 52


def test_manual_dividend_frequency_changes_only_when_star_finds_data() -> None:
    manual = {
        "dividend_frequency": "quarterly",
        "dividend_frequency_source": "manual",
    }
    assert InvestmentWatchService._should_apply_synced_dividend_frequency(
        manual,
        {"dividend_frequency": "unknown"},
    ) is False
    assert InvestmentWatchService._should_apply_synced_dividend_frequency(
        manual,
        {"dividend_frequency": "monthly"},
    ) is True

    no_dividend = InvestmentWatchService._normalized_manual_holding(
        {
            "symbol": "0050",
            "market": "TW",
            "asset_type": "ETF",
            "currency": "TWD",
            "quantity": 1,
            "average_cost": 100,
            "monthly_dividend_twd": 50,
            "dividend_frequency": "none",
        }
    )
    assert no_dividend["estimated_annual_dividend_twd"] == 0
    assert no_dividend["estimated_weekly_dividend_twd"] == 0


def test_investment_analysis_cannot_bypass_star_ai_channel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "test")
    service = InvestmentWatchService(TOOL_ROOT, ai_connections=None)
    try:
        result = service._run_local_risk_ai_for_state(
            {
                "holdings": [
                    {
                        "symbol": "2330",
                        "market": "TW",
                        "quantity": 1,
                    }
                ]
            },
            {},
        )
        assert result["ok"] is False
        assert result["queued"] is False
        assert result["error_code"] == "STAR_AI_CHANNEL_NOT_CONNECTED"

        scheduled = service._schedule_local_risk_ai_background(
            {"holdings": [{"symbol": "2330", "quantity": 1}]},
            {"trigger": "manual_holding_change"},
        )
        assert scheduled["queued"] is False
        assert scheduled["error_code"] == "STAR_AI_CHANNEL_NOT_CONNECTED"
        assert service._background_jobs == set()
    finally:
        service._close_storage()


def test_accounting_is_sent_only_to_star_through_ai_channel() -> None:
    class FakeClient:
        call: tuple[str, str, dict[str, object], int] | None = None

        def request_sync(
            self,
            target: str,
            command: str,
            payload: dict[str, object],
            *,
            timeout_seconds: int,
        ) -> dict[str, object]:
            self.call = (target, command, payload, timeout_seconds)
            return {"ok": True, "apply_reconciliation": False}

    connections = InvestmentAiConnections()
    client = FakeClient()
    connections._client = client

    result = connections.manage_accounting_sync(
        {"differences": []},
        {"transaction_count": 0},
        trigger="test",
    )

    assert result["ok"] is True
    assert client.call is not None
    target, command, payload, timeout = client.call
    assert target == "xingcheng"
    assert command == "xingcheng_manage_investment_accounting"
    assert payload["autonomous"] is True
    assert payload["request_origin"] == "offline-ai-investment-manager"
    assert timeout == 120


def test_external_discussion_is_requested_through_star_only() -> None:
    class FakeClient:
        call: tuple[str, str, dict[str, object], int] | None = None

        def request_sync(
            self,
            target: str,
            command: str,
            payload: dict[str, object],
            *,
            timeout_seconds: int,
        ) -> dict[str, object]:
            self.call = (target, command, payload, timeout_seconds)
            return {
                "ok": True,
                "discussion_owner": "ChatGPT",
                "recipient": "xingcheng",
            }

    connections = InvestmentAiConnections()
    client = FakeClient()
    connections._client = client
    result = connections.discuss_analysis_sync(
        {
            "portfolio": {"active_holding_count": 125},
            "risk_warnings": [],
            "private_database_path": "must-not-cross-channel",
        }
    )

    assert result["ok"] is True
    assert client.call is not None
    target, command, payload, timeout = client.call
    assert target == "xingcheng"
    assert command == "xingcheng_discuss_investment_analysis"
    snapshot = payload["analysis_snapshot"]
    assert isinstance(snapshot, dict)
    assert "private_database_path" not in snapshot
    assert snapshot["database_shared"] is False
    assert timeout == 200


def test_star_autonomous_accounting_is_validated_before_local_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeStarAccounting:
        is_configured = True

        @staticmethod
        def manage_accounting_sync(
            reconciliation: dict[str, object],
            _ledger_summary: dict[str, object],
            *,
            trigger: str,
        ) -> dict[str, object]:
            assert trigger == "test"
            differences = reconciliation.get("differences")
            assert isinstance(differences, list)
            assert len(differences) == 1
            return {
                "ok": True,
                "accounting_owner": "星澄",
                "decision": "apply_estimated_reconciliation",
                "apply_reconciliation": True,
                "approved_action_count": 1,
                "message": "星澄已核准建立估算對帳調整。",
            }

    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "test")
    service = InvestmentWatchService(
        TOOL_ROOT,
        ai_connections=FakeStarAccounting(),
    )
    try:
        service.repository.replace_holdings(
            [
                {
                    "symbol": "2330",
                    "market": "TW",
                    "asset_type": "STOCK",
                    "currency": "TWD",
                    "quantity": 2,
                    "average_cost": 1000,
                    "principal_twd": 2000,
                }
            ],
            change={"action": "test"},
        )

        result = asyncio.run(
            service._run_star_accounting({"trigger": "test"})
        )

        assert result["ok"] is True
        assert result["star_accounting"]["accounting_owner"] == "星澄"
        assert result["ledger_reconciliation"]["applied_count"] == 1
        assert len(service.analytics_store.list_transactions(10)) == 1
    finally:
        service._close_storage()
