from __future__ import annotations

import importlib.util
import asyncio
import os
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

sys.path.insert(
    0,
    os.path.join(os.getcwd(), "src-core"),
)
sys.path.insert(
    0,
    os.path.join(
        os.getcwd(),
        "platform_tools",
        "ai-assistant",
        "src",
        "backend",
        "services",
    ),
)
sys.path.insert(
    0,
    os.path.join(
        os.getcwd(),
        "platform_tools",
        "ai-collaboration",
        "src",
        "backend",
        "services",
    ),
)

from managers.child_tool_service_registry import ChildToolServiceRegistry

from ai_nexus import investment_watch
from ai_nexus import investment_manager_core
from ai_nexus.investment_watch import InvestmentWatchService
from ai_nexus.mobile_sync import DEFAULT_PORT as MOBILE_SYNC_DEFAULT_PORT
from ai_nexus.local_risk_ai import engine as local_risk_ai_engine
from ai_nexus.investment_repository import InvestmentWatchRepository
from ai_nexus.investment_local_ai_upgrade import (
    FundIdentityResolver,
    LocalExplanationEngine,
    build_smart_mapping_repair,
)
from ai_nexus.service import AiNexusService
from ai_collaboration.browser_session import AiCollaborationBrowserSession
from ai_collaboration.repository import AiCollaborationRepository
from ai_collaboration.service import AiCollaborationService


def test_mobile_sync_default_port_does_not_conflict_with_backend_websocket() -> None:
    assert MOBILE_SYNC_DEFAULT_PORT != 8765


def test_repository_recovers_interrupted_ai_runs(tmp_path: Path) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    interrupted = repository.add_ai_run(
        role="local_risk_monitor",
        provider="local-risk-ai",
        prompt="background quote",
        status="running",
    )
    completed = repository.add_ai_run(
        role="local_risk_monitor",
        provider="local-risk-ai",
        prompt="completed quote",
        status="completed",
    )

    assert repository.recover_interrupted_ai_runs() == 1
    runs = {
        run["run_id"]: run for run in repository.load_state().get("ai_runs", [])
    }
    assert runs[interrupted["run_id"]]["status"] == "failed"
    assert "服務重啟" in runs[interrupted["run_id"]]["error"]
    assert runs[completed["run_id"]]["status"] == "completed"


def test_repository_versions_restore_previous_portfolio(tmp_path: Path) -> None:
    repository = InvestmentWatchRepository(tmp_path)
    source = tmp_path / "portfolio.xlsx"
    source.write_bytes(b"xlsx")
    original = [{"symbol": "AAPL", "market": "US", "quantity": 10}]
    saved = repository.save_portfolio(source, original)
    repository.replace_holdings(
        [{**saved["holdings"][0], "quantity": 5}],
        change={"action": "update", "symbol": "AAPL"},
    )

    versions = repository.list_state_versions()
    assert versions and versions[0]["holding_count"] == 1
    restored = repository.restore_state_version(versions[0]["version_id"])
    assert restored["holdings"][0]["quantity"] == 10
    assert restored["restored_from_version"] == versions[0]["version_id"]


def test_fund_identity_resolver_auto_confirms_high_confidence_match() -> None:
    resolver = FundIdentityResolver(
        request_json=lambda _url, _timeout: {
            "quotes": [
                {
                    "symbol": "ABCX",
                    "longname": "Global Growth Fund",
                    "quoteType": "MUTUALFUND",
                    "currency": "USD",
                    "exchange": "NMS",
                }
            ]
        }
    )
    holdings, summary = resolver.resolve_holdings(
        [
            {
                "symbol": "FUND-1",
                "name": "Global Growth Fund",
                "market": "FUND",
                "asset_type": "FUND",
            }
        ]
    )

    assert summary["auto_confirmed_count"] == 1
    assert holdings[0]["fund_quote_symbol"] == "ABCX"
    assert holdings[0]["fund_identity_status"] == "confirmed"


def test_smart_mapping_repair_prefers_consolidated_layout() -> None:
    repair = build_smart_mapping_repair(
        {
            "consolidated_layout": {
                "detected": True,
                "sheet_name": "報酬",
                "holding_count": 254,
            }
        }
    )
    assert repair["recommended"] is True
    assert repair["layout"] == "consolidated_report"
    assert repair["confidence_score"] == 99


def test_local_explanation_guard_rejects_new_claims_and_numbers() -> None:
    facts = "風險偏高。可行動警示 2 項，重大 1 項。資料信心中，請人工確認。"
    assert LocalExplanationEngine._model_text_is_safe(
        "現況風險偏高；資料信心中；請人工確認。",
        facts,
    )
    assert not LocalExplanationEngine._model_text_is_safe(
        "根據經驗表明市場將會上漲 20%，資料信心中，請人工確認。",
        facts,
    )


def test_local_risk_ai_deduplicates_info_and_reuses_incremental_report() -> None:
    checked_at = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    calls = 0

    def quote_function(holding, _providers, _order, now):
        nonlocal calls
        calls += 1
        quote = investment_manager_core.Quote(
            symbol=holding.symbol,
            requested_symbol=holding.symbol,
            provider="test",
            price=100,
            currency="USD",
            as_of=now.isoformat(),
        )
        return quote, [], [quote]

    state = {
        "portfolio": {"file_name": "portfolio.xlsx"},
        "holdings": [
            {
                "symbol": symbol,
                "name": symbol,
                "market": "US",
                "asset_type": "STOCK",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
            for symbol in ("AAA", "BBB")
        ],
    }
    first = local_risk_ai_engine.analyze_state(
        state,
        now=checked_at,
        quote_function=quote_function,
    )
    second = local_risk_ai_engine.analyze_state(
        state,
        now=checked_at + timedelta(minutes=1),
        quote_function=quote_function,
        previous_analysis=first,
    )

    single_source = [
        item for item in first["risk_warnings"] if item["code"] == "quote_single_source"
    ]
    assert len(single_source) == 1
    assert single_source[0]["occurrence_count"] == 2
    assert first["summary"]["warning_count"] == sum(
        1
        for item in first["risk_warnings"]
        if item["severity"] in {"critical", "warning"}
    )
    assert first["summary"]["issue_count"] == first["summary"]["warning_count"] + 1
    assert first["summary"]["source_confidence"]["grade"] == "B"
    assert second["summary"]["reused_holding_count"] == 2
    assert calls == 2


def test_asset_specific_risk_thresholds_are_less_noisy_for_etfs() -> None:
    config = local_risk_ai_engine.RiskRuleConfig()
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    base = {
        "symbol": "TEST",
        "name": "Test",
        "market": "US",
        "quantity": 1,
        "average_cost": 100,
        "unrealized_pnl_percent": -10,
        "status": "quoted",
        "trusted_quote": True,
        "quote": {"price": 90, "currency": "USD"},
    }
    stock_warnings = local_risk_ai_engine.evaluate_risk_warnings(
        [{**base, "asset_type": "STOCK"}], None, now, config
    )
    etf_warnings = local_risk_ai_engine.evaluate_risk_warnings(
        [{**base, "asset_type": "ETF"}], None, now, config
    )
    assert any(item["code"] == "cost_drawdown_critical" for item in stock_warnings)
    assert not any(item["code"].startswith("cost_drawdown") for item in etf_warnings)

def test_local_risk_ai_uses_cached_quotes_and_skips_zero_holdings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_network_quote(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("cached quotes must not call the network provider")

    monkeypatch.setattr(
        investment_manager_core,
        "quote_holding_candidates",
        unexpected_network_quote,
    )
    state = {
        "portfolio": {"file_name": "holdings.xlsx", "holding_count": 2},
        "holdings": [
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 2,
                "average_cost": 180,
                "currency": "USD",
                "web_current_price": 210,
                "web_current_price_currency": "USD",
                "market_data_source": "Yahoo Finance",
                "market_data_updated_at": datetime.now(timezone.utc).isoformat(),
                "source_row": 2,
            },
            {
                "symbol": "MSFT",
                "market": "US",
                "quantity": 0,
                "average_cost": 300,
                "currency": "USD",
                "source_row": 3,
            },
            {
                "symbol": "FUND-R020",
                "name": "美元基金",
                "market": "FUND",
                "asset_type": "FUND",
                "quantity": 2,
                "average_cost": 10,
                "currency": "USD",
                "current_value_twd": 648,
                "average_cost_fx_rate": 32.4,
                "source_row": 4,
            },
        ],
    }
    quote_function = InvestmentWatchService._quote_function_with_cached_market_data(
        state
    )

    result = local_risk_ai_engine.analyze_state(
        state,
        live_quotes=True,
        quote_function=quote_function,
    )

    assert result["summary"]["holding_count"] == 2
    assert result["summary"]["quoted_count"] == 2
    assert result["holdings"][0]["quote"]["provider"] == "Yahoo Finance"
    assert result["holdings"][1]["quote"]["provider"] == "Excel 報酬工作表快照"
    assert result["holdings"][1]["quote"]["price"] == 10.0


def write_inline_xlsx(path: Path, rows: list[list[Any]], sheet_name: str = "自製持股") -> None:
    def column_name(index: int) -> str:
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    sheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{column_name(column_index)}{row_number}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{cell_ref}"><v>{value}</v></c>')
            else:
                cells.append(
                    f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>"
            ),
        )
        workbook.writestr(
            "_rels/.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>"
            ),
        )
        workbook.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>"
            ),
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
        )
        workbook.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetData>{"".join(sheet_rows)}</sheetData>'
                "</worksheet>"
            ),
        )


REQUIRED_TABLES = {
    "ai_nexus_agents",
    "ai_nexus_group_messages",
    "ai_nexus_agent_responses",
    "ai_nexus_workspaces",
    "ai_nexus_tasks",
    "ai_nexus_memory_items",
    "ai_nexus_knowledge_files",
    "ai_nexus_tools",
}


def test_repository_creates_required_schema_and_default_agents(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)

    agents = repository.list_agents()
    with sqlite3.connect(repository.db_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert REQUIRED_TABLES.issubset(tables)
    assert [agent["agent_id"] for agent in agents] == [
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
    ]
    assert all(agent["selected"] == 1 for agent in agents)
    assert all(agent["enabled"] == 1 for agent in agents)


def test_child_tool_registry_splits_investment_and_ai_collaboration_services(tmp_path: Path) -> None:
    registry = ChildToolServiceRegistry(Path.cwd())
    definitions = {definition.service_name: definition for definition in registry.discover()}

    investment_definition = definitions["ai_nexus"]
    collaboration_definition = definitions["ai_collaboration"]

    assert investment_definition.tool_dir_name == "ai-assistant"
    assert investment_definition.class_name == "AiNexusService"
    investment_manifest = json.loads(
        (Path.cwd() / "platform_tools" / "ai-assistant" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    collaboration_manifest = json.loads(
        (Path.cwd() / "platform_tools" / "ai-collaboration" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert investment_manifest["name"] == "AI投資管家"
    assert investment_manifest["version"] == "1.0.0"
    assert collaboration_manifest["name"] == "外部AI協作"
    assert collaboration_manifest["version"] == "1.0.0"

    investment_service = registry.create_service(investment_definition, tmp_path)
    collaboration_service = registry.create_service(collaboration_definition, tmp_path)
    assert not investment_service.owns("ai_nexus_get_state")
    assert not investment_service.owns("ai_nexus_send_message")
    assert investment_service.owns("investment_watch_read_portfolio_file")
    assert investment_service.owns("investment_watch_import_portfolio")
    assert investment_service.owns("investment_watch_run_local_risk_ai")
    assert investment_service.owns("investment_watch_export_report")
    assert collaboration_service.owns("ai_nexus_get_state")
    assert collaboration_service.owns("ai_nexus_send_message")
    assert not collaboration_service.owns("investment_watch_import_portfolio")

    investment_service_dir = (
        Path.cwd()
        / "platform_tools"
        / "ai-assistant"
        / "src"
        / "backend"
        / "services"
        / "ai_nexus"
    )
    investment_watch_source = (investment_service_dir / "investment_watch.py").read_text(
        encoding="utf-8"
    )
    assert "InvestmentBrowserSession" not in investment_watch_source
    assert "send_prompt" not in investment_watch_source
    assert not (investment_service_dir / "browser_session.py").exists()
    assert not (investment_service_dir / "investment_browser_session.py").exists()
    assert not (investment_service_dir / "repository.py").exists()


def test_child_tool_registry_skips_merged_tool_services(tmp_path: Path) -> None:
    merged_tool = tmp_path / "platform_tools" / "merged-investment"
    service_dir = merged_tool / "src" / "backend" / "services" / "investment_watch"
    service_dir.mkdir(parents=True)
    (service_dir / "service.py").write_text(
        "class InvestmentWatchService:\n    pass\n",
        encoding="utf-8",
    )
    (merged_tool / "manifest.json").write_text(
        json.dumps(
            {
                "id": "merged-investment",
                "name": "Merged Investment",
                "merged_into": "ai-assistant",
            }
        ),
        encoding="utf-8",
    )

    registry = ChildToolServiceRegistry(tmp_path)

    assert registry.discover() == []


def test_child_tool_registry_skips_manifest_disabled_services(tmp_path: Path) -> None:
    disabled_tool = tmp_path / "platform_tools" / "disabled-investment"
    service_dir = disabled_tool / "src" / "backend" / "services" / "investment_watch"
    service_dir.mkdir(parents=True)
    (service_dir / "service.py").write_text(
        "class InvestmentWatchService:\n    pass\n",
        encoding="utf-8",
    )
    (disabled_tool / "manifest.json").write_text(
        json.dumps(
            {
                "id": "disabled-investment",
                "name": "Disabled Investment",
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )

    registry = ChildToolServiceRegistry(tmp_path)

    assert registry.discover() == []


def test_investment_watch_module_exports_legacy_portfolio_loader() -> None:
    assert investment_watch.load_portfolio is investment_manager_core.load_portfolio
    assert investment_watch.scan_xlsx_workbook is investment_manager_core.scan_xlsx_workbook
    assert investment_watch.Holding is investment_manager_core.Holding


def test_ai_assistant_main_exports_legacy_investment_watch_api() -> None:
    module_path = (
        Path.cwd()
        / "platform_tools"
        / "ai-assistant"
        / "src"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_gptbridge_investment_watch_main",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.load_portfolio is investment_manager_core.load_portfolio
    assert module.scan_xlsx_workbook is investment_manager_core.scan_xlsx_workbook
    assert module.Holding is investment_manager_core.Holding


def test_repository_persists_group_messages_memory_and_tasks(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)
    repository.save_agent_selection(["chatgpt", "gemini"])

    message = repository.create_group_message("請比較兩個方案", ["chatgpt", "gemini"])
    repository.update_response(
        message["message_id"],
        "chatgpt",
        "completed",
        "方案 A 速度較快。",
    )
    memory = repository.add_memory_item("rule", "語言", "預設使用繁體中文")
    task = repository.create_task(
        "整理比較結論",
        source_message_id=message["message_id"],
        participant_agents=["chatgpt", "gemini"],
    )

    saved_message = repository.get_message(message["message_id"])
    assert saved_message is not None
    assert saved_message["selected_agents"] == ["chatgpt", "gemini"]
    assert len(saved_message["responses"]) == 2
    assert saved_message["responses"][0]["status"] == "completed"
    assert repository.list_memory_items()[0]["memory_id"] == memory["memory_id"]
    assert repository.list_tasks()[0]["task_id"] == task["task_id"]
    assert repository.list_tasks()[0]["participant_agents"] == ["chatgpt", "gemini"]


@pytest.mark.asyncio
async def test_service_group_send_uses_selected_agents_and_records_responses(
    tmp_path: Path,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "profile"

        async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "url": agent["home_url"]}

        async def send_prompt(
            self,
            agent: dict[str, Any],
            prompt: str,
        ) -> dict[str, str]:
            return {
                "status": "completed",
                "content": f"{agent['name']} replied to {prompt}",
                "error": "",
            }

    service = AiCollaborationService(tmp_path, session=FakeSession())
    await service._set_agent_selection({"agent_ids": ["chatgpt", "claude"]})

    result = await service._send_message({"content": "Hello Nexus"})

    assert result["ok"] is True
    group_message = result["group_message"]
    assert group_message["content"] == "Hello Nexus"
    assert group_message["selected_agents"] == ["chatgpt", "claude"]
    assert [response["status"] for response in group_message["responses"]] == [
        "completed",
        "completed",
    ]
    assert "ChatGPT replied" in group_message["responses"][0]["content"]
    assert "Claude replied" in group_message["responses"][1]["content"]


@pytest.mark.asyncio
async def test_service_open_agent_and_state_paths(tmp_path: Path) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

        async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "url": agent["home_url"]}

    service = AiCollaborationService(tmp_path, session=FakeSession())

    open_result = await service._open_agent({"agent_id": "grok"})
    state = await service._get_state({})

    assert open_result["ok"] is True
    assert open_result["url"] == "https://grok.com/"
    assert state["version"] == "1.0.0"
    assert state["database_path"].endswith("runtime\\state\\ai_collaboration.sqlite3") or state[
        "database_path"
    ].endswith("runtime/state/ai_collaboration.sqlite3")
    assert state["browser_profile_path"].endswith("edge-session")
    assert "AI 協作工具" in state["safety_notice"]


@pytest.mark.asyncio
async def test_ai_collaboration_opens_selected_agents_and_exports_diagnostics(
    tmp_path: Path,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"
        is_initialized = True
        pages = {"chatgpt": object()}

        def __init__(self) -> None:
            self.opened: list[str] = []

        async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
            self.opened.append(str(agent["agent_id"]))
            return {"ok": True, "url": agent["home_url"]}

    session = FakeSession()
    service = AiCollaborationService(tmp_path, session=session)
    await service._set_agent_selection({"agent_ids": ["chatgpt", "gemini"]})

    event, payload = await service.handle("ai_nexus_open_selected_agents", {})

    assert event == "ai_nexus_open_selected_agents_result"
    assert payload["ok"] is True
    assert payload["opened"] == 2
    assert session.opened == ["chatgpt", "gemini"]
    assert payload["diagnostics"]["agents"]["selected"] == 2
    assert payload["diagnostics"]["browser"]["initialized"] is True

    export_event, export_payload = await service.handle("ai_nexus_export_report", {})
    report_path = Path(str(export_payload["report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert export_event == "ai_nexus_export_report_result"
    assert export_payload["ok"] is True
    assert report_path.exists()
    assert report["diagnostics"]["agents"]["selected"] == 2
    assert report["diagnostics"]["browser"]["profile_path"].endswith("edge-session")


@pytest.mark.asyncio
async def test_service_routes_integrated_investment_watch_commands(tmp_path: Path) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    service = AiNexusService(tmp_path, session=FakeSession())

    event, payload = await service.handle(
        "investment_watch_clear_state",
        {},
    )

    assert isinstance(service.investment_service, InvestmentWatchService)
    assert service.owns("investment_watch_run_local_risk_ai")
    assert service.owns("investment_watch_get_mobile_sync")
    assert service.owns("investment_watch_set_mobile_sync_remote_url")
    assert service.owns("investment_watch_rotate_mobile_sync_pairing")
    assert service.owns("investment_watch_get_analytics")
    assert service.owns("investment_watch_sync_open_markets")
    assert service.owns("investment_watch_add_transaction")
    assert service.owns("investment_watch_run_backtest")
    assert service.owns("investment_watch_plan_rebalance")
    assert service.owns("investment_watch_revoke_mobile_sync_pairing")
    assert not service.owns("ai_nexus_send_message")
    assert event == "investment_watch_clear_state_result"
    assert payload["ok"] is True
    assert payload["state"]["portfolio"] is None
    assert payload["state"]["holdings"] == []
    assert payload["diagnostics"]["state"] == "setup"
    assert payload["diagnostics"]["boundaries"]["local_only"] is True
    assert payload["diagnostics"]["boundaries"]["external_ai"] is False

    state_event, state_payload = await service.handle("investment_watch_get_state", {})
    assert state_event == "investment_watch_get_state_result"
    assert state_payload["version"] == "1.0.0"
    assert state_payload["state"]["analytics"]["version"] == "4.0.0"
    assert state_payload["analytics_path"].endswith("investment_analytics_v2.sqlite3")
    assert state_payload["local_only"] is True
    assert state_payload["diagnostics"]["boundaries"]["external_ai"] is False
    assert state_payload["state_revision"]
    unchanged_event, unchanged_payload = await service.handle(
        "investment_watch_get_state",
        {"state_revision": state_payload["state_revision"]},
    )
    assert unchanged_event == "investment_watch_get_state_result"
    assert unchanged_payload["ok"] is True
    assert unchanged_payload["not_modified"] is True
    assert "state" not in unchanged_payload

    market_event, market_payload = await service.handle(
        "investment_watch_sync_open_markets",
        {},
    )
    assert market_event == "investment_watch_sync_open_markets_result"
    assert market_payload["market_quote_sync"]["status"] == "no_portfolio"

    export_event, export_payload = await service.handle("investment_watch_export_report", {})
    report_path = Path(str(export_payload["report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert export_event == "investment_watch_export_report_result"
    assert export_payload["ok"] is True
    assert report_path.exists()
    assert report["tool"] == "AI投資管家"
    assert report["redaction_level"] == "support_bundle_default"
    assert "tool_root" not in report
    assert "state_path" not in report
    assert report["diagnostics"]["boundaries"]["local_only"] is True


@pytest.mark.asyncio
async def test_investment_mobile_sync_supports_remote_bridge_and_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_PORT", "0")
    service = AiNexusService(tmp_path, session=None)
    await service.start()
    try:
        event, payload = await service.handle(
            "investment_watch_set_mobile_sync_remote_url",
            {"remote_url": "https://remote.example/ai-investment"},
        )

        assert event == "investment_watch_set_mobile_sync_remote_url_result"
        assert payload["ok"] is True
        status = payload["mobile_sync"]
        assert status["running"] is True
        assert status["remote_ready"] is True
        assert status["mode"] == "remote_bridge"
        assert status["remote_url"].startswith("https://remote.example/ai-investment")
        assert "code=" not in status["remote_url"]
        assert status["bind_host"] == "127.0.0.1"
        old_code = status["pairing_code"]

        rotate_event, rotate_payload = await service.handle(
            "investment_watch_rotate_mobile_sync_pairing",
            {},
        )
        assert rotate_event == "investment_watch_rotate_mobile_sync_pairing_result"
        assert rotate_payload["mobile_sync"]["pairing_code"] != old_code
        assert "code=" not in rotate_payload["mobile_sync"]["remote_url"]

        revoke_event, revoke_payload = await service.handle(
            "investment_watch_revoke_mobile_sync_pairing",
            {},
        )
        assert revoke_event == "investment_watch_revoke_mobile_sync_pairing_result"
        assert revoke_payload["mobile_sync"]["pairing_revoked"] is True
        assert revoke_payload["mobile_sync"]["pairing_code"] == ""
        assert "code=" not in revoke_payload["mobile_sync"]["remote_url"]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_mobile_sync_http_requires_pairing_and_returns_sanitized_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_PORT", "0")
    service = AiNexusService(tmp_path, session=None)
    service.investment_service.repository.save_portfolio(
        tmp_path / "holdings.csv",
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "US",
                "asset_type": "stock",
                "quantity": 2,
                "average_cost": 150,
                "currency": "USD",
            }
        ],
    )
    await service.start()
    try:
        state_event, state_payload = await service.handle("investment_watch_get_state", {})
        assert state_event == "investment_watch_get_state_result"
        status = state_payload["mobile_sync"]
        base_url = f"http://127.0.0.1:{status['port']}"

        with urllib.request.urlopen(f"{base_url}/api/platform", timeout=5) as response:
            platform_payload = json.loads(response.read().decode("utf-8"))
        platform = platform_payload["platform"]
        assert platform_payload["ok"] is True
        assert platform["clients"] == ["desktop", "android_native"]
        assert platform["shared_repository"] is True
        assert platform["source_of_truth"] == "desktop_shared_repository"
        assert platform["foreground_sync_seconds"] == 2
        assert platform["mobile_write_scope"] == "queue_local_ai_command_only"
        compatibility = platform["upgrade_compatibility"]
        assert compatibility["application_version"] == "1.0.0"
        assert compatibility["state_schema"] == {
            "current": 2,
            "minimum_supported": 1,
        }
        assert compatibility["analytics_schema"]["current"] == 5
        assert compatibility["migration_policy"] == "snapshot_then_atomic_migrate"
        assert (
            compatibility["future_schema_policy"]
            == "fail_closed_without_overwrite"
        )

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base_url}/api/state?code=WRONG", timeout=5)
        assert error.value.code == 403

        pair_request = urllib.request.Request(
            f"{base_url}/api/pair",
            data=json.dumps(
                {
                    "code": status["pairing_code"],
                    "client_platform": "android_native",
                    "device_name": "Pixel test",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(pair_request, timeout=5) as response:
            session = json.loads(response.read().decode("utf-8"))
        assert session["ok"] is True
        assert session["session_token"]

        state_request = urllib.request.Request(
            f"{base_url}/api/state",
            headers={"Authorization": f"Bearer {session['session_token']}"},
        )
        with urllib.request.urlopen(state_request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["ok"] is True
        assert payload["state"]["portfolio"]["file_name"] == "holdings.csv"
        assert "source_path" not in payload["state"]["portfolio"]
        assert payload["state"]["holdings"][0]["symbol"] == "AAPL"
        assert payload["platform"]["clients"] == ["desktop", "android_native"]
        assert payload["platform"]["shared_repository"] is True
        assert "pairing_code" not in payload["sync"]
        assert "code=" not in payload["sync"]["loopback_url"]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_investment_read_file_queues_local_risk_ai_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    def fake_quote_holding(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: Any,
        **_kwargs: Any,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote = investment_manager_core.Quote(
            symbol=holding.symbol,
            requested_symbol=holding.symbol,
            provider="fake-local",
            price=150,
            currency="USD",
            previous_close=156,
            change=-6,
            change_percent=-3.85,
            as_of=_now.isoformat(),
            market_state="REGULAR",
        )
        return (
            quote,
            [investment_manager_core.QuoteAttempt("fake-local", True, "ok")],
            [quote],
        )

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding_candidates",
        fake_quote_holding,
    )
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text(
        "symbol,market,quantity,average_cost,currency\nAAPL,US,10,200,USD\n",
        encoding="utf-8",
    )
    service = AiNexusService(tmp_path, session=FakeSession())

    event, payload = await service.handle(
        "investment_watch_read_portfolio_file",
        {"path": str(portfolio)},
    )

    assert event == "investment_watch_read_portfolio_file_result"
    assert payload["ok"] is True
    assert "已讀取" in payload["message"]
    assert "原始檔已釋放" in payload["message"]
    assert "背景自動更新" in payload["message"]
    assert payload["import_mode"] == "snapshot"
    assert payload["source_file_released"] is True
    assert payload["local_risk_ai"]["ok"] is True
    assert payload["local_risk_ai"]["queued"] is True
    assert payload["diagnostics"]["boundaries"]["external_ai"] is False
    assert payload["state"]["ai_runs"][0]["role"] == "local_risk_monitor"
    assert payload["state"]["ai_runs"][0]["status"] == "running"

    await asyncio.gather(
        *list(service.investment_service._background_jobs),
        return_exceptions=False,
    )
    state_event, state_payload = await service.handle("investment_watch_get_state", {})

    assert state_event == "investment_watch_get_state_result"
    assert state_payload["diagnostics"]["local_ai"]["critical_count"] >= 1
    assert state_payload["state"]["ai_runs"][0]["status"] == "completed"
    assert "本地輔助AI監測摘要" in state_payload["state"]["ai_runs"][0]["content"]
    product_status = state_payload["state"]["local_ai_product_status"]
    assert product_status["state"] == "critical"
    assert state_payload["state"]["local_ai_risk_warnings"]
    assert any(
        item["code"] == "cost_drawdown_critical"
        for item in state_payload["state"]["local_ai_risk_warnings"]
    )


@pytest.mark.asyncio
async def test_investment_watch_import_reads_snapshot_not_original_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = tmp_path / "custom holdings.xlsx"
    portfolio.write_bytes(b"snapshot source bytes")
    service = InvestmentWatchService(tmp_path)
    seen_paths: dict[str, Path] = {}

    def fake_scan_xlsx_workbook(path: Path, include_rows: bool = False) -> dict[str, Any]:
        assert include_rows is False
        seen_paths["scan"] = Path(path)
        return {
            "sheet_count": 1,
            "selected_sheet": {
                "sheet_name": "Holdings",
                "header_row_number": 1,
                "header_mode": "direct",
                "score": 120,
                "valid_data_row_count": 1,
            },
            "sheets": [
                {
                    "sheet_name": "Holdings",
                    "usable": True,
                    "score": 120,
                    "header_row_number": 1,
                    "valid_data_row_count": 1,
                }
            ],
        }

    def fake_load_portfolio(path: Path) -> list[investment_manager_core.Holding]:
        seen_paths["load"] = Path(path)
        assert Path(path).read_bytes() == portfolio.read_bytes()
        return [
            investment_manager_core.Holding(
                symbol="AAPL",
                market="US",
                quantity=10,
                average_cost=200,
                currency="USD",
            )
        ]

    def fake_schedule_local_risk_ai_background(
        state: dict[str, Any],
        _payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {"ok": True, "queued": True, "state": state, "product_status": None}

    monkeypatch.setattr(
        investment_manager_core,
        "scan_xlsx_workbook",
        fake_scan_xlsx_workbook,
    )
    monkeypatch.setattr(
        investment_manager_core,
        "load_xlsx_portfolio_consolidated_report",
        lambda _path: (_ for _ in ()).throw(
            investment_manager_core.InvestmentManagerError(
                "No consolidated return worksheet was detected."
            )
        ),
    )
    monkeypatch.setattr(investment_manager_core, "load_portfolio", fake_load_portfolio)
    monkeypatch.setattr(
        service,
        "_schedule_local_risk_ai_background",
        fake_schedule_local_risk_ai_background,
    )

    event, payload = await service.handle(
        "investment_watch_read_portfolio_file",
        {"path": str(portfolio)},
    )

    snapshot = seen_paths["load"]
    assert event == "investment_watch_read_portfolio_file_result"
    assert payload["ok"] is True
    assert seen_paths["scan"] == snapshot
    assert snapshot != portfolio.resolve()
    assert snapshot.parent == service.repository.runtime_root / "imports"
    assert snapshot.exists()
    assert payload["state"]["portfolio"]["source_path"] == str(portfolio.resolve())
    assert payload["state"]["portfolio"]["file_name"] == portfolio.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("async_method_name", "sync_method_name", "payload"),
    [
        ("_preview_excel_mapping", "_preview_excel_mapping_sync", {}),
        (
            "_import_excel_mapping",
            "_import_excel_mapping_sync",
            {"refresh_quotes": False},
        ),
        ("_read_portfolio_file", "_read_portfolio_file_sync", {}),
    ],
)
async def test_investment_excel_workers_keep_event_loop_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    async_method_name: str,
    sync_method_name: str,
    payload: dict[str, Any],
) -> None:
    service = InvestmentWatchService(tmp_path)
    if async_method_name == "_import_excel_mapping":
        source = tmp_path / "responsive-worker.xlsx"
        source.write_bytes(b"responsive worker fixture")
        payload = {**payload, "path": str(source)}

    def slow_worker(_payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.15)
        return {"ok": False, "message": "synthetic worker result"}

    monkeypatch.setattr(service, sync_method_name, slow_worker)
    operation = asyncio.create_task(
        getattr(service, async_method_name)(payload)
    )
    started = time.perf_counter()
    await asyncio.sleep(0.03)
    event_loop_delay = time.perf_counter() - started

    assert event_loop_delay < 0.1
    assert operation.done() is False
    assert (await operation)["message"] == "synthetic worker result"
    await service.shutdown()


@pytest.mark.asyncio
async def test_investment_watch_manual_excel_mapping_previews_and_imports_without_locking(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "custom broker.xlsx"
    write_inline_xlsx(
        portfolio,
        [
            ["券商庫存"],
            ["部位", "品名", "識別碼", "區域", "均價", "幣種"],
            [12, "Apple", "AAPL", "美股", 180, "USD"],
            [1000, "台積電", "2330", "台股", 590, "TWD"],
        ],
        sheet_name="庫存明細",
    )
    original = portfolio.read_bytes()
    service = InvestmentWatchService(tmp_path)

    _event, preview_result = await service.handle(
        "investment_watch_preview_excel_mapping",
        {"path": str(portfolio)},
    )
    preview = preview_result["excel_mapping_preview"]

    assert preview_result["ok"] is True
    assert preview_result["source_file_released"] is True
    assert preview["selected_sheet_name"] == "庫存明細"
    assert preview["sheets"][0]["rows"][1][2] == "識別碼"

    _event, imported = await service.handle(
        "investment_watch_import_excel_mapping",
        {
            "path": str(portfolio),
            "sheet_name": "庫存明細",
            "header_row_number": 2,
            "data_start_row_number": 3,
            "column_mapping": {
                "quantity": 0,
                "name": 1,
                "symbol": 2,
                "market": 3,
                "average_cost": 4,
                "currency": 5,
            },
            "refresh_quotes": False,
        },
    )

    assert imported["ok"] is True
    assert imported["source_file_released"] is True
    assert imported["source_file_modified"] is False
    assert [item["symbol"] for item in imported["state"]["holdings"]] == ["AAPL", "2330"]
    assert imported["state"]["excel_import_profile"]["column_mapping"]["symbol"] == 2
    assert imported["state"]["excel_import_profile"]["data_start_row_number"] == 3
    assert imported["state"]["workbook_scan"]["selected_sheet"]["header_mode"] == "manual_mapping"
    assert portfolio.read_bytes() == original

    versions_before_retry = service.repository.list_state_versions(limit=100)
    imported_at = imported["state"]["portfolio"]["imported_at"]
    _event, retried = await service.handle(
        "investment_watch_import_excel_mapping",
        {
            "path": str(portfolio),
            "sheet_name": "庫存明細",
            "header_row_number": 2,
            "data_start_row_number": 3,
            "column_mapping": {
                "quantity": 0,
                "name": 1,
                "symbol": 2,
                "market": 3,
                "average_cost": 4,
                "currency": 5,
            },
            "refresh_quotes": False,
        },
    )
    assert retried["deduplicated"] is True
    assert retried["state"]["portfolio"]["imported_at"] == imported_at
    assert (
        service.repository.list_state_versions(limit=100)
        == versions_before_retry
    )

    renamed = portfolio.with_name("renamed.xlsx")
    portfolio.rename(renamed)
    renamed.rename(portfolio)
    assert portfolio.read_bytes() == original


@pytest.mark.asyncio
async def test_investment_watch_imports_consolidated_return_sheet_with_254_style_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = tmp_path / "return-report.xlsx"

    def report_row(**values: Any) -> list[Any]:
        indexes = {
            "b": 1,
            "c": 2,
            "d": 3,
            "e": 4,
            "f": 5,
            "g": 6,
            "h": 7,
            "n": 13,
            "o": 14,
            "s": 18,
            "v": 21,
        }
        output: list[Any] = [""] * 22
        for key, value in values.items():
            output[indexes[key]] = value
        return output

    rows: list[list[Any]] = [[] for _ in range(47)]
    rows[1] = report_row(c="名稱", n="總單位數", s="最新淨值")
    rows[2] = report_row(c="美元基金", f=10, g=0.12, n=2, s=20, v=1000)
    rows[43] = report_row(c="名稱", s="即時股價")
    rows[44] = report_row(b="0050", c="元大台灣50", n=2, o=300, s=180, v=360)
    rows[45] = report_row(b="Apple", c="AAPL", n=3, s=200, v=19500)
    rows[46] = report_row(b="現金", c="國泰", n=1, v=5000)
    write_inline_xlsx(portfolio, rows, sheet_name="報酬 ")
    original = portfolio.read_bytes()
    service = InvestmentWatchService(tmp_path)

    _event, preview_result = await service.handle(
        "investment_watch_preview_excel_mapping",
        {"path": str(portfolio)},
    )
    consolidated = preview_result["excel_mapping_preview"]["consolidated_layout"]
    assert consolidated["holding_count"] == 3

    _event, imported = await service.handle(
        "investment_watch_import_excel_mapping",
        {
            "path": str(portfolio),
            "layout": "consolidated_report",
            "config": consolidated,
            "refresh_quotes": False,
        },
    )

    assert imported["ok"] is True
    assert imported["import_mode"] == "snapshot_consolidated_report"
    assert len(imported["state"]["holdings"]) == 3
    assert imported["state"]["excel_import_profile"]["layout"] == "consolidated_report"
    assert imported["state"]["holdings"][0]["estimated_weekly_dividend_twd"] == pytest.approx(
        120 / 52
    )
    assert portfolio.read_bytes() == original

    monkeypatch.setattr(
        service,
        "_schedule_local_risk_ai_background",
        lambda state, _payload: {
            "ok": True,
            "queued": True,
            "state": state,
            "product_status": None,
        },
    )
    _event, automatic = await service.handle(
        "investment_watch_read_portfolio_file",
        {"path": str(portfolio)},
    )

    assert automatic["ok"] is True
    assert automatic["import_mode"] == "snapshot_consolidated_report"
    assert len(automatic["state"]["holdings"]) == 3
    assert automatic["state"]["excel_import_profile"]["layout"] == "consolidated_report"


def test_import_symbol_quality_rejects_bad_mapping_before_save(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    invalid = [
        {"symbol": f"錯誤名稱 {index}", "quantity": index}
        for index in range(5)
    ]

    with pytest.raises(
        investment_manager_core.InvestmentManagerError,
        match="已取消匯入並保留原有資料",
    ):
        service._assert_import_symbol_quality(invalid)


@pytest.mark.asyncio
async def test_investment_watch_automatically_logs_local_ai_errors(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)

    _event, result = await service.handle(
        "investment_watch_import_excel_mapping",
        {},
    )

    assert result["ok"] is False
    assert result["error_logged"] is True
    assert result["error_id"] in result["message"]
    error_log = service.repository.runtime_root / "local-ai-errors.jsonl"
    assert error_log.exists()
    record = json.loads(error_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["error_id"] == result["error_id"]
    assert "message" not in record
    assert "payload" not in record
    assert "traceback" not in record
    assert "message_protected" in record


def test_investment_diagnostics_prioritizes_excel_mapping_errors(tmp_path: Path) -> None:
    service = InvestmentWatchService(tmp_path)
    diagnostics = service._diagnostics(
        {
            "portfolio": {"file_name": "bad.xlsx"},
            "holdings": [
                {"symbol": "17.24"},
                {"symbol": "164.94"},
                {"symbol": "10.61"},
                {"symbol": "AAPL"},
            ],
            "local_ai_product_status": {"state": "critical"},
            "local_ai_summary": {"critical_count": 10},
            "local_ai_risk_warnings": [],
            "ai_runs": [],
        }
    )

    assert diagnostics["state_label"] == "Excel 欄位需修正"
    assert diagnostics["data_quality"]["risk_analysis_suspended"] is True
    assert "代號看起來像價格" in diagnostics["message"]

def test_local_risk_ai_analyze_portfolio_file_retains_audit_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text(
        "symbol,market,quantity,average_cost,currency\nAAPL,US,10,200,USD\n",
        encoding="utf-8",
    )
    seen_paths: dict[str, Path] = {}

    def fake_create_snapshot(
        source: Path,
        _imports_root: Path,
        *,
        keep: int,
    ) -> Path:
        assert source == portfolio
        assert keep == investment_manager_core.DEFAULT_IMPORT_SNAPSHOT_KEEP
        snapshot = tmp_path / "runtime-snapshot.csv"
        snapshot.write_bytes(portfolio.read_bytes())
        seen_paths["snapshot"] = snapshot
        return snapshot

    def fake_load_portfolio(path: Path) -> list[investment_manager_core.Holding]:
        seen_paths["load"] = Path(path)
        assert Path(path).exists()
        return [
            investment_manager_core.Holding(
                symbol="AAPL",
                market="US",
                quantity=10,
                average_cost=200,
                currency="USD",
            )
        ]

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "create_portfolio_file_snapshot",
        fake_create_snapshot,
    )
    monkeypatch.setattr(local_risk_ai_engine.core, "load_portfolio", fake_load_portfolio)

    result = local_risk_ai_engine.analyze_portfolio_file(portfolio, live_quotes=False)

    assert seen_paths["load"] == seen_paths["snapshot"]
    assert seen_paths["snapshot"].exists()
    assert result["retained_snapshot_path"] == str(seen_paths["snapshot"])
    assert result["snapshot_retention"] == "append_only"
    assert result["portfolio"]["source_path"] == str(portfolio)
    assert result["portfolio"]["file_name"] == portfolio.name


def test_local_risk_ai_can_run_without_external_ai() -> None:
    holding = investment_manager_core.Holding(
        symbol="TSLA",
        market="US",
        quantity=5,
        average_cost=250,
        currency="USD",
    )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=False,
    )

    assert result["mode"] == "local-offline"
    assert result["summary"]["holding_count"] == 1
    assert result["risk_warnings"]
    assert "本地輔助AI監測摘要" in result["content"]
    assert "本地AI產品狀態" in result["content"]
    assert "本地AI評分與策略" in result["content"]
    assert result["assistant_ready"] is True
    product_status = result["product_status"]
    assert product_status["state"] in {"attention", "critical", "ready"}
    assert product_status["offline_mode"] is True
    assert product_status["command_suggestions"]
    assert product_status["next_actions"]
    assert product_status["decision_summary"]
    assert product_status["confidence_label"] in {"高", "中", "低"}
    assert product_status["action_count"] >= 1
    assert product_status["trigger_count"] >= 1
    assessment = result["local_ai_assessment"]
    assert assessment["portfolio_score"] > 0
    assert assessment["portfolio_rating"]["rating"]
    assert assessment["memory_update"]["kind"] == "investment-local-risk-ai"
    assert assessment["decision_brief"]
    assert assessment["confidence_summary"]["label"] in {"高", "中", "低"}
    assert assessment["action_plan"]
    assert assessment["watch_triggers"]
    assert "本地AI判讀" in assessment["memory_update"]["content"]
    holding_assessment = assessment["assessments"][0]
    assert holding_assessment["symbol"] == "TSLA"
    assert holding_assessment["score"] > 0
    assert holding_assessment["scenario"]["base"]
    assert holding_assessment["strategy"]
    assert holding_assessment["score_components"]
    framework = result["analysis_framework"]
    assert framework["workflow"][0] == "公司與產業定位"
    assert sum(item["max_score"] for item in framework["dynamic_score_model"]) == 100
    assert sum(item["weight"] for item in framework["chief_baseline_weighting"]) == 100
    assert framework["active_weighting"]["profile"] in {
        "常態市場基準模式",
        "高估值防禦模式",
    }
    assert framework["rating_standards"][0]["rating"] == "★★★★★"


def test_local_risk_ai_command_returns_structured_command_result() -> None:
    holding = investment_manager_core.Holding(
        symbol="AAPL",
        market="US",
        quantity=10,
        average_cost=200,
        currency="USD",
    )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=False,
        instruction="評分 AAPL 停損 8% 盤中 2% 停利 25% 資料過期 24小時",
    )

    command = result["command"]
    assert command["intent"] == "score"
    assert command["symbols"] == ["AAPL"]
    assert "score" in command["sections"]
    assert command["config"]["critical_loss_percent"] == -8
    assert command["config"]["intraday_drop_percent"] == -2
    assert command["config"]["large_gain_percent"] == 25
    assert command["config"]["stale_portfolio_hours"] == 24
    assert result["command_result"]["intent"] == "score"
    assert "評分/權重" in result["command_result"]["text"]
    assert "本地AI行動計畫" in result["command_result"]["text"]
    assert "監測觸發條件" in result["command_result"]["text"]
    assert "本地AI投組評分" in result["command_result"]["text"]
    assert "投資情境與操作策略" in result["command_result"]["text"]
    assert result["command_result"]["portfolio_score"] > 0
    assert result["command_result"]["action_plan"]
    assert result["command_result"]["watch_triggers"]
    assert result["command_result"]["confidence_summary"]["label"] in {"高", "中", "低"}
    assert result["command_result"]["assessments"][0]["symbol"] == "AAPL"
    assert any("AAPL" in item for item in result["product_status"]["command_suggestions"])
    assert "命令結果" in result["content"]


def test_local_risk_ai_plan_command_builds_action_plan_and_triggers() -> None:
    holding = investment_manager_core.Holding(
        symbol="NVDA",
        market="US",
        quantity=3,
        average_cost=100,
        currency="USD",
    )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=False,
        instruction="行動計畫 NVDA 再平衡 監測觸發 停損線",
    )

    command = result["command"]
    command_result = result["command_result"]

    assert command["intent"] == "plan"
    assert "plan" in command["sections"]
    assert "triggers" in command["sections"]
    assert command_result["action_plan"]
    assert command_result["watch_triggers"]
    assert command_result["decision_brief"]
    assert "本地AI行動計畫" in command_result["text"]
    assert "監測觸發條件" in command_result["text"]


def test_local_risk_ai_marks_partial_online_quote_health() -> None:
    holdings = [
        investment_manager_core.Holding(
            symbol="AAPL",
            market="US",
            quantity=2,
            average_cost=180,
            currency="USD",
        ),
        investment_manager_core.Holding(
            symbol="MSFT",
            market="US",
            quantity=1,
            average_cost=400,
            currency="USD",
        ),
    ]

    def fake_quote_holding(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: Any,
    ) -> tuple[investment_manager_core.Quote | None, list[investment_manager_core.QuoteAttempt]]:
        if holding.symbol == "AAPL":
            return (
                investment_manager_core.Quote(
                    symbol=holding.symbol,
                    requested_symbol=holding.symbol,
                    provider="fake-online",
                    price=190,
                    currency="USD",
                ),
                [investment_manager_core.QuoteAttempt("fake-online", True, "ok")],
            )
        return (
            None,
            [investment_manager_core.QuoteAttempt("fake-online", False, "timeout")],
        )

    result = local_risk_ai_engine.analyze_holdings(
        holdings,
        live_quotes=True,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    network_context = result["network_context"]
    assert network_context["health"] == "attention"
    assert network_context["health_label"] == "部分報價"
    assert network_context["quoted_count"] == 1
    assert network_context["failed_quote_count"] == 1
    assert network_context["quote_gap_count"] == 1
    assert network_context["quote_gaps"][0]["symbol"] == "MSFT"
    assert network_context["quote_gaps"][0]["reason"] == "所有報價來源失敗"
    assert "timeout" in network_context["quote_gaps"][0]["detail"]
    assert result["product_status"]["quote_health"] == "attention"
    assert result["product_status"]["network_context"]["quote_gaps"][0]["symbol"] == "MSFT"
    assert result["command_result"]["network_context"]["coverage_percent"] == 50.0
    assert result["command_result"]["network_context"]["quote_gap_count"] == 1
    assert "網路資料：已啟用，1 / 2 報價成功。" in result["command_result"]["text"]


def test_local_risk_ai_marks_high_coverage_missing_quotes_as_partial() -> None:
    holdings = [
        investment_manager_core.Holding(
            symbol=f"T{i:03d}",
            market="US",
            quantity=1,
            average_cost=10,
            currency="USD",
        )
        for i in range(124)
    ]
    holdings.extend(
        [
            investment_manager_core.Holding(
                symbol="MISS1",
                market="US",
                quantity=1,
                average_cost=10,
                currency="USD",
            ),
            investment_manager_core.Holding(
                symbol="MISS2",
                market="US",
                quantity=1,
                average_cost=10,
                currency="USD",
            ),
        ]
    )

    def fake_quote_holding(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: Any,
    ) -> tuple[investment_manager_core.Quote | None, list[investment_manager_core.QuoteAttempt]]:
        if holding.symbol.startswith("MISS"):
            return (
                None,
                [investment_manager_core.QuoteAttempt("fake-online", False, "not found")],
            )
        return (
            investment_manager_core.Quote(
                symbol=holding.symbol,
                requested_symbol=holding.symbol,
                provider="fake-online",
                price=12,
                currency="USD",
            ),
            [investment_manager_core.QuoteAttempt("fake-online", True, "ok")],
        )

    result = local_risk_ai_engine.analyze_holdings(
        holdings,
        live_quotes=True,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    network_context = result["network_context"]
    assert network_context["coverage_label"] == "124 / 126 報價成功"
    assert network_context["health"] == "attention"
    assert network_context["health_label"] == "部分報價"
    assert network_context["quote_gap_count"] == 2
    assert [item["symbol"] for item in network_context["quote_gaps"]] == ["MISS1", "MISS2"]


def test_local_risk_ai_accepts_cross_verified_quotes() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    holding = investment_manager_core.Holding(
        symbol="AAPL",
        market="US",
        quantity=1,
        average_cost=95,
        currency="USD",
    )

    def fake_quote_holding(
        _holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: datetime,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote_a = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-a",
            price=100,
            currency="USD",
            as_of=now.isoformat(),
        )
        quote_b = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-b",
            price=100.4,
            currency="USD",
            as_of=now.isoformat(),
        )
        return (
            quote_a,
            [
                investment_manager_core.QuoteAttempt("source-a", True, "ok"),
                investment_manager_core.QuoteAttempt("source-b", True, "ok"),
            ],
            [quote_a, quote_b],
        )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=True,
        now=now,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    assert result["holdings"][0]["status"] == "quoted"
    assert result["holdings"][0]["quote_validation"]["state"] == "verified"
    assert result["network_context"]["health"] == "ready"
    assert result["network_context"]["health_label"] == "交叉驗證通過"
    assert result["network_context"]["cross_checked_count"] == 1
    assert result["product_status"]["quote_health_label"] == "交叉驗證通過"


def test_ai_investment_manager_uses_local_device_time(tmp_path: Path) -> None:
    local_offset = datetime.now().astimezone().utcoffset()

    core_now = investment_manager_core.utc_now()
    assert core_now.tzinfo is not None
    assert core_now.utcoffset() == local_offset

    repository = InvestmentWatchRepository(tmp_path)
    state = repository.save_portfolio(
        tmp_path / "holdings.csv",
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
        ],
    )
    imported_at = datetime.fromisoformat(str(state["portfolio"]["imported_at"]))
    updated_at = datetime.fromisoformat(str(state["updated_at"]))

    assert imported_at.utcoffset() == local_offset
    assert updated_at.utcoffset() == local_offset


def test_local_risk_ai_rejects_divergent_cross_source_quotes() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    holding = investment_manager_core.Holding(
        symbol="AAPL",
        market="US",
        quantity=1,
        average_cost=100,
        currency="USD",
    )

    def fake_quote_holding(
        _holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: datetime,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote_a = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-a",
            price=100,
            currency="USD",
            as_of=now.isoformat(),
        )
        quote_b = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-b",
            price=105,
            currency="USD",
            as_of=now.isoformat(),
        )
        return (
            quote_a,
            [
                investment_manager_core.QuoteAttempt("source-a", True, "ok"),
                investment_manager_core.QuoteAttempt("source-b", True, "ok"),
            ],
            [quote_a, quote_b],
        )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=True,
        now=now,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    assert result["holdings"][0]["status"] == "quote_untrusted"
    assert result["holdings"][0]["market_value"] is None
    assert result["network_context"]["health"] == "critical"
    assert result["network_context"]["untrusted_quote_count"] == 1
    assert result["network_context"]["divergence_count"] == 1
    assert any(item["code"] == "quote_divergence_critical" for item in result["risk_warnings"])
    assert "報價驗證：報價異常，交叉驗證 1 檔，異常 1 檔。" in result["command_result"]["text"]


def test_local_risk_ai_rejects_stale_intraday_quotes() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    stale_time = now - timedelta(minutes=30)
    holding = investment_manager_core.Holding(
        symbol="AAPL",
        market="US",
        quantity=1,
        average_cost=100,
        currency="USD",
    )

    def fake_quote_holding(
        _holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: datetime,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote_a = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-a",
            price=101,
            currency="USD",
            as_of=stale_time.isoformat(),
        )
        quote_b = investment_manager_core.Quote(
            symbol="AAPL",
            requested_symbol="AAPL",
            provider="source-b",
            price=101.1,
            currency="USD",
            as_of=stale_time.isoformat(),
        )
        return (
            quote_a,
            [
                investment_manager_core.QuoteAttempt("source-a", True, "ok"),
                investment_manager_core.QuoteAttempt("source-b", True, "ok"),
            ],
            [quote_a, quote_b],
        )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=True,
        now=now,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    assert result["holdings"][0]["status"] == "quote_untrusted"
    assert result["network_context"]["stale_quote_count"] == 1
    assert any(item["code"] == "quote_stale" for item in result["risk_warnings"])


def test_local_risk_ai_rejects_symbol_mismatched_quotes() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    holding = investment_manager_core.Holding(
        symbol="AAPL",
        market="US",
        quantity=1,
        average_cost=100,
        currency="USD",
    )

    def fake_quote_holding(
        _holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: datetime,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote_a = investment_manager_core.Quote(
            symbol="MSFT",
            requested_symbol="MSFT",
            provider="source-a",
            price=100,
            currency="USD",
            as_of=now.isoformat(),
        )
        quote_b = investment_manager_core.Quote(
            symbol="MSFT",
            requested_symbol="MSFT",
            provider="source-b",
            price=100.1,
            currency="USD",
            as_of=now.isoformat(),
        )
        return (
            quote_a,
            [
                investment_manager_core.QuoteAttempt("source-a", True, "ok"),
                investment_manager_core.QuoteAttempt("source-b", True, "ok"),
            ],
            [quote_a, quote_b],
        )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=True,
        now=now,
        quote_function=fake_quote_holding,
        instruction="連網 摘要 持股",
    )

    assert result["holdings"][0]["status"] == "quote_untrusted"
    assert result["network_context"]["symbol_mismatch_count"] == 1
    assert any(item["code"] == "quote_symbol_mismatch" for item in result["risk_warnings"])


@pytest.mark.asyncio
async def test_local_risk_ai_command_filters_holdings_and_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    def fake_quote_holding(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: Any,
        **_kwargs: Any,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote = investment_manager_core.Quote(
            symbol=holding.symbol,
            requested_symbol=holding.symbol,
            provider="fake-local",
            price=194 if holding.symbol == "AAPL" else 100,
            currency="USD",
            previous_close=200,
            change=-6,
            change_percent=-3,
            as_of=_now.isoformat(),
            market_state="REGULAR",
        )
        return (
            quote,
            [investment_manager_core.QuoteAttempt("fake-local", True, "ok")],
            [quote],
        )

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding_candidates",
        fake_quote_holding,
    )
    service = AiNexusService(tmp_path, session=FakeSession())
    service.investment_service.repository.save_portfolio(
        tmp_path / "holdings.csv",
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 10,
                "average_cost": 200,
                "currency": "USD",
            },
            {
                "symbol": "MSFT",
                "market": "US",
                "quantity": 10,
                "average_cost": 100,
                "currency": "USD",
            },
        ],
    )

    event, payload = await service.handle(
        "investment_watch_run_local_risk_ai",
        {
            "instruction": "只看 AAPL 跌破成本 3% 集中度 90%",
            "live_quotes": True,
        },
    )

    assert event == "investment_watch_run_local_risk_ai_result"
    assert payload["ok"] is True
    assert payload["local_risk_ai"]["command"]["symbols"] == ["AAPL"]
    assert payload["local_risk_ai"]["holdings"][0]["symbol"] == "AAPL"
    assert len(payload["local_risk_ai"]["holdings"]) == 1
    assert payload["product_status"]["command_suggestions"]
    assert any(
        str(item).startswith("連網")
        for item in payload["product_status"]["command_suggestions"]
    )
    assert payload["state"]["local_ai_product_status"]["state_label"]
    assert any(
        item["code"] == "cost_drawdown_warning"
        for item in payload["state"]["local_ai_risk_warnings"]
    )
    assert payload["state"]["local_ai_command_result"]["symbols"] == ["AAPL"]
    assert payload["state"]["local_ai_action_plan"]
    assert payload["state"]["local_ai_watch_triggers"]
    assert payload["state"]["local_ai_confidence"]["label"] in {"高", "中", "低"}
    assert payload["state"]["local_ai_decision_brief"]
    assert payload["state"]["local_ai_command_result"]["action_plan"]
    assert payload["state"]["local_ai_command_result"]["watch_triggers"]
    assert "使用者命令" in payload["run"]["prompt"]
    assert "只看 AAPL" in payload["run"]["content"]
    assert any(
        item["code"] == "cost_drawdown_warning"
        for item in payload["local_risk_ai"]["risk_warnings"]
    )


@pytest.mark.asyncio
async def test_local_risk_ai_command_defaults_to_online_quotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    quote_calls: list[str] = []

    def fake_quote_holding(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _provider_order: list[str],
        _now: datetime,
        **_kwargs: Any,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote_calls.append(holding.symbol)
        quote = investment_manager_core.Quote(
            symbol=holding.symbol,
            requested_symbol=holding.symbol,
            provider="fake-online",
            price=510,
            currency="USD",
            change_percent=1.2,
            as_of=_now.isoformat(),
        )
        return (
            quote,
            [investment_manager_core.QuoteAttempt("fake-online", True, "ok")],
            [quote],
        )

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding_candidates",
        fake_quote_holding,
    )
    service = AiNexusService(tmp_path, session=FakeSession())
    service.investment_service.repository.save_portfolio(
        tmp_path / "holdings.csv",
        [
            {
                "symbol": "SPY",
                "market": "US",
                "asset_type": "ETF",
                "quantity": 1,
                "average_cost": 500,
                "currency": "USD",
            }
        ],
    )

    event, payload = await service.handle(
        "investment_watch_run_local_risk_ai",
        {"instruction": "高估值 通膨 防禦"},
    )

    assert event == "investment_watch_run_local_risk_ai_result"
    assert payload["ok"] is True
    assert "報價：1 / 1 報價成功" in payload["message"]
    assert quote_calls == ["SPY"]
    assert payload["local_risk_ai"]["mode"] == "local-live"
    assert payload["local_risk_ai"]["analysis_framework"]["active_weighting"][
        "profile"
    ] == "高估值防禦模式"
    assert payload["state"]["local_ai_product_status"]["offline_mode"] is False
    assert payload["state"]["local_ai_product_status"]["network_enabled"] is True
    assert payload["state"]["local_ai_product_status"]["quote_health"] == "attention"
    assert payload["state"]["local_ai_product_status"]["quote_health_label"] == "單一來源"
    assert payload["state"]["local_ai_product_status"]["network_context"]["quoted_count"] == 1
    assert payload["state"]["local_ai_network_context"]["quoted_count"] == 1
    assert payload["state"]["local_ai_network_context"]["health"] == "attention"
    assert payload["state"]["local_ai_network_context"]["single_source_count"] == 1
    assert payload["state"]["local_ai_network_context"]["cross_checked_count"] == 0
    assert payload["state"]["local_ai_network_context"]["quote_providers"] == ["fake-online"]
    assert payload["state"]["local_ai_command_result"]["network_context"]["mode"] == "online-quotes"
    assert "memory_items" not in payload
    assert payload["state"]["local_ai_command_result"]


@pytest.mark.asyncio
async def test_local_risk_ai_offline_command_disables_online_quotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    def fail_quote_holding(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("offline local command should not request live quotes")

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding",
        fail_quote_holding,
    )
    service = AiNexusService(tmp_path, session=FakeSession())
    service.investment_service.repository.save_portfolio(
        tmp_path / "holdings.csv",
        [
            {
                "symbol": "SPY",
                "market": "US",
                "asset_type": "ETF",
                "quantity": 1,
                "average_cost": 500,
                "currency": "USD",
            }
        ],
    )

    event, payload = await service.handle(
        "investment_watch_run_local_risk_ai",
        {"instruction": "離線 高估值 通膨 防禦"},
    )

    assert event == "investment_watch_run_local_risk_ai_result"
    assert payload["ok"] is True
    assert payload["local_risk_ai"]["mode"] == "local-offline"
    assert payload["state"]["local_ai_product_status"]["offline_mode"] is True
    assert payload["state"]["local_ai_product_status"]["network_enabled"] is False
    assert payload["state"]["local_ai_network_context"]["enabled"] is False
    assert payload["state"]["local_ai_network_context"]["health"] == "offline"


@pytest.mark.asyncio
async def test_ai_group_message_writes_auto_shared_memory(tmp_path: Path) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

        async def send_prompt(self, agent: dict[str, Any], content: str) -> dict[str, Any]:
            return {
                "status": "completed",
                "content": f"{agent['name']} 回覆：{content}",
            }

    service = AiCollaborationService(tmp_path, session=FakeSession())

    event, payload = await service.handle(
        "ai_nexus_send_message",
        {"content": "記住我的投資偏好", "agent_ids": ["chatgpt"]},
    )

    assert event == "ai_nexus_send_message_result"
    assert payload["ok"] is True
    assert payload["auto_memory_item"]["kind"] == "auto"
    assert payload["memory_items"][0]["memory_id"] == payload["auto_memory_item"]["memory_id"]
    assert "自動共享記憶" in payload["auto_memory_item"]["content"]


def test_local_risk_ai_dynamic_weights_for_high_volatility_assets() -> None:
    holding = investment_manager_core.Holding(
        symbol="BTC",
        market="CRYPTO",
        asset_type="crypto",
        quantity=1,
        average_cost=50000,
        currency="USD",
    )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=False,
    )
    dynamic_scores = {
        item["name"]: item["max_score"]
        for item in result["analysis_framework"]["dynamic_score_model"]
    }

    assert sum(dynamic_scores.values()) == 100
    assert dynamic_scores["風險"] > 5
    assert dynamic_scores["技術面"] > 10
    active_weights = {
        item["name"]: item["weight"]
        for item in result["analysis_framework"]["active_weighting"]["weights"]
    }
    assert result["analysis_framework"]["active_weighting"]["profile"] == "高估值防禦模式"
    assert active_weights["資產配置與風控"] == 45
    assert active_weights["總體經濟"] == 10
    assert result["analysis_framework"]["dynamic_weight_notes"]
    assert "動態權重調整" in result["content"]


def test_local_risk_ai_defensive_weighting_from_command() -> None:
    holding = investment_manager_core.Holding(
        symbol="SPY",
        market="US",
        asset_type="ETF",
        quantity=1,
        average_cost=500,
        currency="USD",
    )

    result = local_risk_ai_engine.analyze_holdings(
        [holding],
        live_quotes=False,
        instruction="高估值 通膨 防禦",
    )
    active_weights = {
        item["name"]: item["weight"]
        for item in result["analysis_framework"]["active_weighting"]["weights"]
    }

    assert result["analysis_framework"]["active_weighting"]["profile"] == "高估值防禦模式"
    assert active_weights == {
        "企業基本面": 40,
        "資產配置與風控": 45,
        "總體經濟": 10,
        "動能與趨勢": 5,
    }


def test_investment_repository_saves_workbook_scan_quality(tmp_path: Path) -> None:
    repository = InvestmentWatchRepository(tmp_path)

    state = repository.save_portfolio(
        tmp_path / "custom-holdings.xlsx",
        [
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 10,
                "average_cost": 200,
                "currency": "USD",
            }
        ],
        workbook_scan={
            "sheet_count": 2,
            "selected_sheet": {
                "sheet_name": "自製持股",
                "header_row_number": 4,
                "header_mode": "inferred",
                "header_depth": 2,
                "score": 188,
                "valid_data_row_count": 8,
            },
            "sheets": [
                {"sheet_name": "摘要", "usable": False, "score": 0},
                {"sheet_name": "自製持股", "usable": True, "score": 188},
            ],
        },
    )

    quality = state["workbook_scan_quality"]
    assert quality["state"] == "ready"
    assert quality["selected_sheet_name"] == "自製持股"
    assert quality["header_mode"] == "inferred"
    assert "自製表格" in quality["recommendation"]
    assert "Workbook scan quality" in state["shared_memory"]


def test_xlsx_scanner_imports_headerless_custom_workbook(tmp_path: Path) -> None:
    workbook = tmp_path / "headerless-custom.xlsx"
    write_inline_xlsx(
        workbook,
        [
            ["AAPL", "Apple", "US", 10, 200, "USD"],
            ["2330", "台積電", "TW", 1000, 600, "TWD"],
        ],
    )

    scan = investment_manager_core.scan_xlsx_workbook(workbook)
    selected = scan["selected_sheet"]
    holdings = investment_manager_core.load_xlsx_portfolio(workbook)

    assert selected["header_mode"] == "headerless_inferred"
    assert selected["header_row_index"] == -1
    assert selected["header_row_number"] is None
    assert selected["data_start_row_number"] == 1
    assert selected["valid_data_row_count"] == 2
    assert [holding.symbol for holding in holdings] == ["AAPL", "2330"]
    assert holdings[0].quantity == 10
    assert holdings[1].average_cost == 600


def test_ai_assistant_frontend_exposes_local_ai_product_status() -> None:
    ui_root = Path.cwd() / "platform_tools" / "ai-assistant" / "src" / "ui"
    app_source = (ui_root / "AiAssistantWindowApp.tsx").read_text(encoding="utf-8")
    feature_source = (ui_root / "investmentWatchFeature.tsx").read_text(encoding="utf-8")
    css_source = (ui_root / "ai-assistant.css").read_text(encoding="utf-8")

    assert "星澄分析 · 外部 AI 討論" in app_source
    assert "星澄負責搜尋與投資分析" in app_source
    assert "持倉風險預告" in app_source
    assert "星澄命令" in app_source
    assert "星澄行動計畫" in app_source
    assert "監測觸發條件" in app_source
    assert "星澄決策摘要" in app_source
    assert "報價資料" in app_source
    assert "報價驗證" in app_source
    assert "報價缺口" in app_source
    assert "localAiQuoteGaps" in app_source
    assert "讀取檔案" in app_source
    assert "investmentHoldings.slice(0, 36)" not in app_source
    assert "筆持股未顯示" not in app_source
    assert "socketStatus !== 'Connected'" in app_source
    assert "診斷同步中" in app_source
    assert "localAiQuoteHealthLabel" in app_source
    assert "localAiVerificationLabel" in app_source
    assert "匯出去識別診斷" in app_source
    assert "LOCAL_AI_COMMAND_PRESETS" in app_source
    assert "nexus-diagnostics-panel" in app_source
    assert "ai_nexus_send_message" not in app_source
    assert "investment_watch_run_all_primary_agents" not in app_source
    assert "investment_watch_gemini_quote_search" not in app_source
    assert "investment_watch_export_report" in feature_source
    assert "investment_watch_read_portfolio_file" in feature_source
    assert "readPortfolioFile" in feature_source
    assert "window.setInterval(refreshState, 5000)" in app_source
    assert "window.setInterval(refreshState, 1000)" not in app_source
    assert "後端連線已中斷，操作結果未知" in app_source
    assert "window.addEventListener('socket_connected', connectionHandler)" in app_source
    assert "investmentDiagnostics" in feature_source
    assert "buildClientInvestmentDiagnostics" in feature_source
    assert "investmentDiagnosticsFromResult" in feature_source
    assert "setInvestmentDiagnostics(result.diagnostics || null)" not in feature_source
    assert "workbook_scan_quality" in feature_source
    assert "local_ai_risk_warnings" in feature_source
    assert "local_ai_command_result" in feature_source
    assert "local_ai_action_plan" in feature_source
    assert "local_ai_watch_triggers" in feature_source
    assert "local_ai_confidence" in feature_source
    assert "local_ai_network_context" in feature_source
    assert "quote_gaps" in feature_source
    assert "quote_gap_count" in feature_source
    assert "quote_health_label" in feature_source
    assert "cross_checked_count" in feature_source
    assert "workbook_scan" in feature_source
    assert "local_ai_product_status" in feature_source
    assert "nexus-diagnostics-panel" in css_source
    assert "nexus-command-presets" in css_source
    assert "nexus-action-plan" in css_source
    assert "nexus-watch-triggers" in css_source
    assert "nexus-local-ai-decision" in css_source
    assert "nexus-network-pill" in css_source
    assert "nexus-quote-gaps" in css_source
    assert "repeat(auto-fit, minmax(138px, 1fr))" in css_source
    assert "repeat(auto-fit, minmax(118px, 1fr))" in css_source
    assert "nexus-risk-board" in css_source
    assert "nexus-command-result" in css_source
    assert "nexus-task" not in css_source


def test_ai_collaboration_frontend_is_independent_from_investment_manager() -> None:
    ui_root = Path.cwd() / "platform_tools" / "ai-collaboration" / "src" / "ui"
    app_source = (ui_root / "AiCollaborationWindowApp.tsx").read_text(encoding="utf-8")
    css_source = (ui_root / "ai-collaboration.css").read_text(encoding="utf-8")

    assert "AI協作工具" in app_source
    assert "AI 名單" in app_source
    assert "ai_nexus_send_message" in app_source
    assert "ai_nexus_add_memory" in app_source
    assert "ai_nexus_open_selected_agents" in app_source
    assert "ai_nexus_export_report" in app_source
    assert "PROMPT_PRESETS" in app_source
    assert "investment_watch_" not in app_source
    assert "ai-collab-agent" in css_source
    assert "ai-collab-diagnostics" in css_source


@pytest.mark.asyncio
async def test_browser_session_launches_edge_in_background_without_forbidden_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    lifecycle = {"context_closed": False, "playwright_stopped": False}

    class FakePage:
        url = "about:blank"

        def is_closed(self) -> bool:
            return False

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url

    class FakeContext:
        pages: list[FakePage] = []

        async def new_page(self) -> FakePage:
            page = FakePage()
            self.pages.append(page)
            return page

        def on(self, _event: str, _callback: object) -> None:
            return None

        async def close(self) -> None:
            lifecycle["context_closed"] = True

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
            captured.update(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            lifecycle["playwright_stopped"] = True

    class FakePlaywrightStarter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(
        "ai_collaboration.browser_session.async_playwright",
        lambda: FakePlaywrightStarter(),
    )

    session = AiCollaborationBrowserSession(profile_root=tmp_path / "profiles")
    page = await session.ensure_agent_page(
        {
            "agent_id": "chatgpt",
            "home_url": "https://chatgpt.com/",
        }
    )

    assert page.url == "https://chatgpt.com/"
    assert captured["channel"] == "msedge"
    assert captured["headless"] is True
    assert captured["user_data_dir"].endswith("ai-collaboration\\shared") or captured[
        "user_data_dir"
    ].endswith("ai-collaboration/shared")
    assert "args" not in captured
    assert "user_agent" not in captured

    await session.close_background_context()

    assert lifecycle == {"context_closed": True, "playwright_stopped": True}
    assert session.context is None
    assert session.playwright is None
    assert session.is_initialized is False
