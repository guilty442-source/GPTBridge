"""Split from consolidated test_xingcheng.py (local-model/tests/test_model_registry.py)."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT
from _test_model_registry_helpers import _make_service, _make_transformer_service

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any
import pytest
from xingcheng.domain.model_registry import StarModelRegistry
from xingcheng.domain.module_registry import StarModuleRegistry
from xingcheng.integration.memory_broker import StarMemoryBroker
from xingcheng.infrastructure.repository import LocalAiRepository
from xingcheng.infrastructure.ollama_model_repository import OllamaModelRepository
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.model_engines import StarModelEngines
from xingcheng.infrastructure.generative_language_model import (
    StarAutoregressiveLanguageModel,
)
from xingcheng.infrastructure.native_model import StarNativeLanguageModel
from xingcheng.infrastructure import repository as repository_module
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.market_data import (
    MarketDataSearch,
    _fund_query_terms,
    _yahoo_symbol_candidates,
    market_source_catalog,
    recognize_holding_identity,
)
from xingcheng.application.investment_accounting import coordinate_investment_accounting
from xingcheng.application.investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from xingcheng.application.coding_expert import StarCodingExpert

def test_main_model_understands_and_executes_a_search_instruction(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    service.market_data.search = lambda payload: {
        "ok": True,
        "searched_at": "2026-08-12T00:00:00+00:00",
        "provider": "test-offline-source",
        "requested_count": len(payload.get("holdings") or []),
        "updated_count": 1,
        "error_count": 0,
        "results": [],
        "errors": [],
    }

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "instruction": "請查詢這個標的的公開資料",
                "holdings": [{"symbol": "TEST", "asset_type": "FUND"}],
            },
        )
    )

    assert result["intent"] == "search"
    assert result["model_role"] == "daily-primary"
    assert result["market_research"]["provider"] == "test-offline-source"
    assert result["instruction_execution"]["executed"] is True
    assert result["instruction_execution"]["status"] == "completed"


def test_star_expands_market_search_with_bounded_symbol_candidates() -> None:
    assert _yahoo_symbol_candidates("2330", "TW") == ["2330.TW", "2330.TWO"]
    assert _yahoo_symbol_candidates("700", "HK") == ["0700.HK"]
    assert _yahoo_symbol_candidates("SHOP", "CA") == ["SHOP.TO", "SHOP.V"]
    assert _yahoo_symbol_candidates("AAPL", "US") == ["AAPL"]
    assert len(_fund_query_terms("摩根士丹利環球機會基金美元A級別")) <= 8


def test_star_recognizes_symbol_market_and_fund_identifiers() -> None:
    stock = recognize_holding_identity(
        {"symbol": "tw:2330", "name": "台積電", "market": "AUTO"}
    )
    assert stock["symbol"] == "2330"
    assert stock["market"] == "TW"
    assert stock["recognition"]["status"] == "recognized"

    fund = recognize_holding_identity(
        {
            "symbol": "FUND-001",
            "name": "全球收益基金美元A級別",
            "fund_isin": "LU 1234-5678",
        }
    )
    assert fund["market"] == "FUND"
    assert fund["asset_type"] == "FUND"
    assert fund["isin"] == "LU1234-5678"


def test_star_resolves_unknown_symbol_from_name_before_quote_search() -> None:
    def fetch_json(url: str, _body: dict[str, object] | None = None) -> dict[str, object]:
        if "finance/search" in url:
            return {
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "longname": "Apple Inc.",
                        "quoteType": "EQUITY",
                        "exchange": "NMS",
                        "currency": "USD",
                    }
                ]
            }
        if "/chart/AAPL?" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 220,
                                "regularMarketTime": 1786492800,
                                "currency": "USD",
                                "longName": "Apple Inc.",
                            },
                            "timestamp": [1786406400, 1786492800],
                            "indicators": {"quote": [{"close": [218, 220]}]},
                            "events": {"dividends": {}},
                        }
                    ]
                }
            }
        return {"chart": {"result": [], "error": {"description": "not found"}}}

    search = MarketDataSearch(fetch_json=fetch_json)
    result = search._search_holding(
        {
            "symbol": "UNKNOWN",
            "name": "Apple Inc.",
            "market": "OTHER",
            "currency": "USD",
        }
    )

    assert result["ok"] is True
    assert result["resolved_symbol"] == "AAPL"
    assert result["identity_resolution"]["source"] == "Yahoo Finance Search"
    assert result["verification"]["manual_update_allowed"] is True


def test_star_market_sources_cover_dividend_price_and_nav() -> None:
    catalog = market_source_catalog()
    fields = {field for source in catalog for field in source["fields"]}
    source_ids = {source["id"] for source in catalog}

    assert fields == {"dividend", "price", "nav"}
    assert {"twse-openapi", "tpex-openapi", "fundclear", "mops", "sitca"} <= source_ids
    assert {"yahoo-finance", "morningstar", "moneydj", "google-search"} <= source_ids
    assert {"hkex", "jpx", "sec-edgar", "nasdaq"} <= source_ids
    assert next(item for item in catalog if item["id"] == "google-search")["role"] == "discovery-only"


def test_tw_official_quote_overrides_public_quote_and_adds_source() -> None:
    def fetch_json(url: str, _body: dict[str, object] | None = None) -> dict[str, object]:
        if url.endswith("STOCK_DAY_ALL"):
            return {
                "data": [
                    {
                        "Date": "1150812",
                        "Code": "2330",
                        "ClosingPrice": "1200",
                        "Change": "10",
                    }
                ]
            }
        if url.endswith("TWT48U_ALL"):
            return {
                "data": [
                    {
                        "Date": "1150814",
                        "Code": "2330",
                        "CashDividend": "5",
                    }
                ]
            }
        if "tpex_" in url:
            return {"data": []}
        if "query1.finance.yahoo.com" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 1190,
                                "regularMarketTime": 1786492800,
                                "currency": "TWD",
                            },
                            "timestamp": [1786406400, 1786492800],
                            "indicators": {"quote": [{"close": [1180, 1190]}]},
                            "events": {"dividends": {}},
                        }
                    ]
                }
            }
        return {"data": []}

    search = MarketDataSearch(fetch_json=fetch_json)
    result = search._search_holding(
        {"symbol": "2330", "name": "台積電", "market": "TW", "asset_type": "STOCK"}
    )

    assert result["ok"] is True
    assert result["parameters"]["price"] == 1200
    assert result["parameters"]["previous_close"] == 1190
    assert result["distribution_events"][0]["amount_per_unit"] == 5
    assert [source["name"] for source in result["sources"]][:2] == [
        "臺灣證券交易所",
        "Yahoo Finance",
    ]


def test_star_reviews_chatgpt_advice_before_updating_investment_parameters(
    tmp_path: Path,
) -> None:
    service = _make_transformer_service(tmp_path)
    service.transformer_runtime.generate = lambda **_kwargs: {
        "ok": True,
        "text": """
        [
          {"parameter_key":"max_single_position_percent","value":18,"rationale":"降低集中度"},
          {"parameter_key":"source_confidence_minimum","value":2,"rationale":"超出範圍"},
          {"parameter_key":"unknown_parameter","value":1,"rationale":"未授權"}
        ]
        """,
    }

    _event, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {"prompt": "參考本機模型建議調整投資參數"},
        )
    )

    assert result["ok"] is True
    assert result["advisor"] == "deepseek-r1:14b"
    assert result["external_ai_used"] is False
    assert result["database_owner"] == "star-investment-native-model"
    assert result["current_parameters"]["max_single_position_percent"] == 18
    assert result["current_parameters"]["source_confidence_minimum"] == 0.72
    assert result["rejected_count"] == 2
    assert result["applied"][0]["previous_value"] == 20


def test_all_registered_investment_models_execute_with_evidence() -> None:
    result = analyze_investments(
        {
            "holdings": [
                {
                    "symbol": "TEST",
                    "name": "測試基金",
                    "quantity": 10,
                    "principal_twd": 900,
                    "current_value_twd": 1000,
                    "currency": "TWD",
                    "region": "TW",
                    "industry": "Technology",
                    "asset_type": "FUND",
                    "market_data_source": "official-test",
                    "market_data_source_url": "https://example.test/official",
                    "market_data_updated_at": "2026-08-20T00:00:00+00:00",
                    "market_parameters": {
                        "price": 100,
                        "previous_close": 95,
                        "ytd_return_percent": 8,
                        "volatility_percent": 18,
                        "management_fee_percent": 0.8,
                        "custody_fee_percent": 0.2,
                        "distribution_yield_percent": 4,
                        "risk_level": "RR3",
                    },
                }
            ]
        }
    )

    assert set(result["model_results"]) == set(ANALYSIS_MODEL_KEYS)
    assert result["model_execution"]["executed_model_count"] == 8
    assert result["model_results"]["scenario-stress"]["metrics"]["estimated_loss_twd"] > 0
    assert result["model_results"]["fee-efficiency"]["metrics"]["estimated_annual_fee_twd"] == 10
    assert result["evidence"][0]["source"] == "official-test"


def test_star_semantic_plan_extracts_multi_intent_entities(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    plan = service.native_model.semantic_plan(
        "請分析台股 2330 的長期風險，再計算 XIRR 與相關性"
    )

    assert {"risk", "analysis", "calculation", "statistics"} <= set(plan["intents"])
    assert "2330" in plan["entities"]["symbols"]
    assert "TW" in plan["entities"]["markets"]
    assert plan["entities"]["time_horizon"] == "long"
    assert all(task["confidence"] >= 0.62 for task in plan["tasks"])


def test_mathematical_expert_executes_portfolio_metrics_xirr_and_rebalancing(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請計算 XIRR、夏普與再平衡",
                "cash_flows": [
                    {"date": "2024-01-01", "amount": -1000},
                    {"date": "2025-01-01", "amount": 1100},
                ],
                "returns": [0.01, -0.005, 0.02, 0.004],
                "prices": [100, 105, 90, 120],
                "portfolio_value": 1000,
                "current_weights": {"stock": 70, "bond": 30},
                "target_weights": {"stock": 60, "bond": 40},
                "scenario_shocks": {"stock": -20, "bond": -5},
            },
        )
    )

    mathematical = result["mathematical_result"]
    assert mathematical["xirr"]["annual_rate_percent"] == pytest.approx(10, abs=0.1)
    assert mathematical["portfolio_metrics"]["maximum_drawdown_percent"] < 0
    assert mathematical["scenario_and_rebalancing"]["rebalance_trade_values"]["stock"] == -100
    assert mathematical["audit_trace"]["reproducible"] is True


def test_external_memory_requires_review_and_can_be_revoked(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    pending = service.memory_broker.accept_external_candidates(
        [
            {
                "candidate_id": "review-1",
                "content": "待人工核對的外部結論",
                "source_agent_id": "chatgpt",
                "status": "candidate",
            }
        ],
        business_scope="investment",
        task_type="orchestration",
    )
    memory_id = pending[0]["memory_id"]
    assert service.memory_broker.context_for_external("investment", "analysis") == []

    _, approved = asyncio.run(
        service.handle(
            "xingcheng_memory_review",
            {"memory_id": memory_id, "action": "approve", "reviewer": "owner"},
        )
    )
    assert approved["ok"] is True
    assert service.memory_broker.context_for_external("investment", "analysis")

    _, revoked = asyncio.run(
        service.handle(
            "xingcheng_memory_review",
            {"memory_id": memory_id, "action": "revoke", "reviewer": "owner"},
        )
    )
    assert revoked["ok"] is True
    assert service.memory_broker.context_for_external("investment", "analysis") == []


def test_dynamic_upgrade_evaluation_checks_live_components(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _, evaluation = asyncio.run(service.handle("xingcheng_evaluate_upgrade", {}))
    health = service.runtime_health()

    assert evaluation["ok"] is True
    assert evaluation["checks"]["registered_investment_models_executable"] is True
    assert evaluation["checks"]["mathematical_capabilities_ready"] is True
    assert evaluation["checks"]["sqlite_integrity_verified"] is True
    assert evaluation["metrics"]["executable_investment_model_count"] == 8
    assert evaluation["external_research_health"]["fail_closed"] is True
    assert health["analysis_model_count"] == 8
    assert health["mathematical_capability_count"] == 16
    assert health["memory_review_required"] is True


def test_intent_classifier_respects_negation_and_multilingual_requests() -> None:
    chinese_plan = StarNativeLanguageModel.semantic_plan(
        "不要搜尋，只要分析我提供的持股"
    )
    english_plan = StarNativeLanguageModel.semantic_plan(
        "Do not search; only analyze the supplied holdings"
    )

    assert chinese_plan["intents"] == ["analysis"]
    assert chinese_plan["prohibited_intents"] == ["search"]
    assert english_plan["intents"] == ["analysis"]
    assert english_plan["prohibited_intents"] == ["search"]
    assert set(
        StarNativeLanguageModel.classify_intents(
            "Calculate 2+2 and summarize the attached document"
        )
    ) == {"calculation", "reading"}


def test_general_conversation_and_capability_questions_use_distinct_intents() -> None:
    assert StarNativeLanguageModel.classify_intents("嗨") == ["conversation"]
    assert StarNativeLanguageModel.classify_intents(
        "這是直接連線測試，請簡短回覆指定文字"
    )[0] == "conversation"
    assert StarNativeLanguageModel.classify_intents("你有哪些能力？") == [
        "capabilities"
    ]


def test_model_review_and_repair_request_routes_to_self_upgrade() -> None:
    plan = StarNativeLanguageModel.semantic_plan("檢討星澄語言模型並修正")

    assert plan["primary_intent"] == "self_upgrade"
    assert "status" in plan["intents"]


def test_user_repair_and_programming_commands_are_understood() -> None:
    repair = StarNativeLanguageModel.semantic_plan("檢討星澄並修正")
    coding = StarNativeLanguageModel.semantic_plan("請修正問題並修改程式")

    assert repair["primary_intent"] == "self_upgrade"
    assert repair["comprehension"]["command"]["execution_requested"] is True
    assert coding["primary_intent"] == "coding"
    assert coding["comprehension"]["command"]["execution_requested"] is True


def test_self_upgrade_command_executes_bounded_maintenance(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    _, result = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "檢討星澄並修正"})
    )

    assert result["intent"] == "self_upgrade"
    assert result["self_repair"]["executed"] is False
    assert result["self_repair"]["frozen"] is True
    assert result["self_repair"]["governance_rule_modified"] is False
    assert result["instruction_execution"]["executed"] is False


def test_external_collaboration_obeys_explicit_and_natural_language_denial() -> None:
    assert LocalAiService._plan_external_collaboration(
        "請用 AI 協作搜尋資料", {"use_external_collaboration": False}
    ) == []
    assert LocalAiService._plan_external_collaboration(
        "不要使用外部 AI，只要星澄分析", {}
    ) == []


def test_approved_relevant_memory_is_grounded_but_not_self_trained(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    pending = service.memory_broker.accept_external_candidates(
        [
            {
                "candidate_id": "budget-fact-1",
                "kind": "analysis",
                "title": "專案預算限制",
                "content": "專案預算上限為NT$500,000。",
                "source_agent_id": "chatgpt",
                "status": "candidate",
            }
        ],
        business_scope="general",
        task_type="conversation",
    )
    memory_id = pending[0]["memory_id"]
    service.memory_broker.review_memory(
        memory_id,
        action="approve",
        reviewer="test-owner",
        reason="verified source",
    )

    listed = service.memory_broker.list_memories()
    assert any(item["memory_id"] == memory_id for item in listed)
    approved = next(item for item in listed if item["memory_id"] == memory_id)
    assert approved["review_status"] == "approved"
    assert approved["source_type"] == "external-ai-candidate"

    main_repository = service.repositories[service.models.MAIN.model_id]
    tables = main_repository.database_status()["tables"]
    assert tables["model_memory"] >= 1
    assert tables["language_training_example"] == 0

    context = service.memory_broker.context_for_inference(
        "general",
        "conversation",
        "請說明專案預算限制",
        owner_only=False,
    )
    assert any(item["memory_id"] == memory_id for item in context)


def test_model_repository_closes_every_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(repository_module.sqlite3, "connect", tracked_connect)
    repository = LocalAiRepository(tmp_path, database_scope="main")
    repository.database_status()
    repository.memory_context("general")

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_ollama_models_have_isolated_owned_databases(tmp_path: Path) -> None:
    gemma_id = "gemma4:e2b-it-qat"
    qwen_id = "qwen3.5:9b-q4_K_M"
    gemma = OllamaModelRepository(tmp_path, gemma_id)
    qwen = OllamaModelRepository(tmp_path, qwen_id)

    gemma.record_inference(
        intent="conversation",
        model_role="daily-primary",
        request={"prompt": "hello"},
        response={"ok": True, "model": gemma_id},
    )

    assert gemma.database_path != qwen.database_path
    assert gemma.status()["owner_model_id"] == gemma_id
    assert gemma.status()["tables"]["inference_record"] == 1
    assert qwen.status()["tables"]["inference_record"] == 0
    assert gemma.status()["investment_database_access"] is False
    with pytest.raises(
        PermissionError, match="OLLAMA_MODEL_DATABASE_ISOLATION_DENIED"
    ):
        gemma.record_inference(
            intent="search",
            model_role="search-agent",
            request={},
            response={"ok": True, "model": qwen_id},
        )
