from __future__ import annotations

import importlib.util
import os
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

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

from managers.child_tool_service_registry import ChildToolServiceRegistry

from ai_nexus import investment_watch
from ai_nexus.browser_session import AiNexusBrowserSession
from ai_nexus import investment_manager_core
from ai_nexus.investment_watch import InvestmentWatchService
from ai_nexus.local_risk_ai import engine as local_risk_ai_engine
from ai_nexus.investment_repository import InvestmentWatchRepository
from ai_nexus.repository import AiNexusRepository
from ai_nexus.service import AiNexusService


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
    repository = AiNexusRepository(tmp_path)

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


def test_child_tool_registry_discovers_ai_assistant_group_service(tmp_path: Path) -> None:
    registry = ChildToolServiceRegistry(Path.cwd())
    definitions = [
        definition
        for definition in registry.discover()
        if definition.service_name == "ai_nexus"
    ]

    assert len(definitions) == 1
    assert definitions[0].tool_dir_name == "ai-assistant"
    assert definitions[0].class_name == "AiNexusService"
    manifest = json.loads(
        (Path.cwd() / "platform_tools" / "ai-assistant" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["name"] == "AI投資管家"
    assert manifest["version"] == "1.4.0"

    service = registry.create_service(definitions[0], tmp_path)
    assert service.owns("ai_nexus_get_state")
    assert service.owns("ai_nexus_send_message")
    assert service.owns("investment_watch_import_portfolio")
    assert service.owns("investment_watch_run_local_risk_ai")


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
    repository = AiNexusRepository(tmp_path)
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

    service = AiNexusService(tmp_path, session=FakeSession())
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

    service = AiNexusService(tmp_path, session=FakeSession())

    open_result = await service._open_agent({"agent_id": "grok"})
    state = await service._get_state({})

    assert open_result["ok"] is True
    assert open_result["url"] == "https://grok.com/"
    assert state["version"] == "0.1.0"
    assert state["database_path"].endswith("runtime\\state\\ai_nexus.sqlite3") or state[
        "database_path"
    ].endswith("runtime/state/ai_nexus.sqlite3")
    assert state["browser_profile_path"].endswith("edge-session")
    assert "Microsoft Edge Stable" in state["safety_notice"]


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
    assert service.investment_service.browser.session is service.session
    assert event == "investment_watch_clear_state_result"
    assert payload["ok"] is True
    assert payload["state"]["portfolio"] is None
    assert payload["state"]["holdings"] == []


@pytest.mark.asyncio
async def test_investment_import_auto_runs_local_risk_ai(
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
    ) -> tuple[investment_manager_core.Quote, list[investment_manager_core.QuoteAttempt]]:
        return (
            investment_manager_core.Quote(
                symbol=holding.symbol,
                requested_symbol=holding.symbol,
                provider="fake-local",
                price=150,
                currency="USD",
                previous_close=156,
                change=-6,
                change_percent=-3.85,
                market_state="REGULAR",
            ),
            [investment_manager_core.QuoteAttempt("fake-local", True, "ok")],
        )

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding",
        fake_quote_holding,
    )
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text(
        "symbol,market,quantity,average_cost,currency\nAAPL,US,10,200,USD\n",
        encoding="utf-8",
    )
    service = AiNexusService(tmp_path, session=FakeSession())

    event, payload = await service.handle(
        "investment_watch_import_portfolio",
        {"path": str(portfolio)},
    )

    assert event == "investment_watch_import_portfolio_result"
    assert payload["ok"] is True
    assert payload["local_risk_ai"]["ok"] is True
    assert payload["state"]["ai_runs"][0]["role"] == "local_risk_monitor"
    assert "本地輔助AI監測摘要" in payload["state"]["ai_runs"][0]["content"]
    product_status = payload["local_risk_ai"]["product_status"]
    assert product_status["state"] == "critical"
    assert payload["product_status"]["state"] == product_status["state"]
    assert payload["state"]["local_ai_product_status"]["state"] == product_status["state"]
    assert payload["state"]["local_ai_risk_warnings"]
    assert any(
        item["code"] == "cost_drawdown_critical"
        for item in payload["local_risk_ai"]["risk_warnings"]
    )


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
    assessment = result["local_ai_assessment"]
    assert assessment["portfolio_score"] > 0
    assert assessment["portfolio_rating"]["rating"]
    assert assessment["memory_update"]["kind"] == "investment-local-risk-ai"
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
    assert "本地AI投組評分" in result["command_result"]["text"]
    assert "投資情境與操作策略" in result["command_result"]["text"]
    assert result["command_result"]["portfolio_score"] > 0
    assert result["command_result"]["assessments"][0]["symbol"] == "AAPL"
    assert any("AAPL" in item for item in result["product_status"]["command_suggestions"])
    assert "命令結果" in result["content"]


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
    ) -> tuple[investment_manager_core.Quote, list[investment_manager_core.QuoteAttempt]]:
        return (
            investment_manager_core.Quote(
                symbol=holding.symbol,
                requested_symbol=holding.symbol,
                provider="fake-local",
                price=194 if holding.symbol == "AAPL" else 100,
                currency="USD",
                previous_close=200,
                change=-6,
                change_percent=-3,
                market_state="REGULAR",
            ),
            [investment_manager_core.QuoteAttempt("fake-local", True, "ok")],
        )

    monkeypatch.setattr(
        local_risk_ai_engine.core,
        "quote_holding",
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
    assert payload["state"]["local_ai_product_status"]["state_label"]
    assert any(
        item["code"] == "cost_drawdown_warning"
        for item in payload["state"]["local_ai_risk_warnings"]
    )
    assert payload["state"]["local_ai_command_result"]["symbols"] == ["AAPL"]
    assert "使用者命令" in payload["run"]["prompt"]
    assert "只看 AAPL" in payload["run"]["content"]
    assert any(
        item["code"] == "cost_drawdown_warning"
        for item in payload["local_risk_ai"]["risk_warnings"]
    )


@pytest.mark.asyncio
async def test_local_risk_ai_command_defaults_to_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

    def fail_quote_holding(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("manual local command should not request live quotes")

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
        {"instruction": "高估值 通膨 防禦"},
    )

    assert event == "investment_watch_run_local_risk_ai_result"
    assert payload["ok"] is True
    assert payload["local_risk_ai"]["mode"] == "local-offline"
    assert payload["local_risk_ai"]["analysis_framework"]["active_weighting"][
        "profile"
    ] == "高估值防禦模式"
    assert payload["state"]["local_ai_product_status"]["offline_mode"] is True
    assert payload["memory_items"][0]["kind"] == "auto-investment"
    assert "自動共享記憶" in payload["memory_items"][0]["content"]


@pytest.mark.asyncio
async def test_ai_group_message_writes_auto_shared_memory(tmp_path: Path) -> None:
    class FakeSession:
        shared_profile_dir = tmp_path / "edge-session"

        async def send_prompt(self, agent: dict[str, Any], content: str) -> dict[str, Any]:
            return {
                "status": "completed",
                "content": f"{agent['name']} 回覆：{content}",
            }

    service = AiNexusService(tmp_path, session=FakeSession())

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


def test_ai_assistant_frontend_exposes_local_ai_product_status() -> None:
    ui_root = Path.cwd() / "platform_tools" / "ai-assistant" / "src" / "ui"
    app_source = (ui_root / "AiAssistantWindowApp.tsx").read_text(encoding="utf-8")
    feature_source = (ui_root / "investmentWatchFeature.tsx").read_text(encoding="utf-8")
    css_source = (ui_root / "ai-assistant.css").read_text(encoding="utf-8")

    assert "本地AI狀態" in app_source
    assert "風險預告" in app_source
    assert "最近命令結果" in app_source
    assert "命令提示" in app_source
    assert "workbook_scan_quality" in feature_source
    assert "local_ai_risk_warnings" in feature_source
    assert "local_ai_command_result" in feature_source
    assert "workbook_scan" in feature_source
    assert "local_ai_product_status" in feature_source
    assert "nexus-risk-board" in css_source
    assert "nexus-command-result" in css_source
    assert "nexus-task" not in css_source


@pytest.mark.asyncio
async def test_browser_session_launches_edge_stable_without_forbidden_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

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

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
            captured.update(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakePlaywrightStarter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(
        "ai_nexus.browser_session.async_playwright",
        lambda: FakePlaywrightStarter(),
    )

    session = AiNexusBrowserSession(profile_root=tmp_path / "profiles")
    page = await session.ensure_agent_page(
        {
            "agent_id": "chatgpt",
            "home_url": "https://chatgpt.com/",
        }
    )

    assert page.url == "https://chatgpt.com/"
    assert captured["channel"] == "msedge"
    assert captured["headless"] is False
    assert captured["user_data_dir"].endswith("ai-assistant\\shared") or captured[
        "user_data_dir"
    ].endswith("ai-assistant/shared")
    assert "args" not in captured
    assert "user_agent" not in captured
