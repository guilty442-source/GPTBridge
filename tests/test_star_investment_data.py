from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_AI_SERVICES = (
    ROOT / "platform_tools" / "local-ai" / "src" / "backend" / "services"
)
if str(LOCAL_AI_SERVICES) not in sys.path:
    sys.path.insert(0, str(LOCAL_AI_SERVICES))

from local_ai.external_research import ExternalBrowserResearch
from local_ai.investment_analysis import analyze_investments
from local_ai.market_data import FUNDCLEAR_SEARCH_URL, MarketDataSearch
from local_ai.repository import LocalAiRepository
from local_ai.service import LocalAiService
from local_ai.upgrade_evaluation import evaluate_star_upgrade


def test_star_resolves_internal_fund_to_official_nav_and_distribution_evidence() -> None:
    def fake_fetch(url: str, body: dict | None = None) -> dict:
        if url == FUNDCLEAR_SEARCH_URL:
            return {
                "data": [
                    {
                        "fundCode": "93110333B",
                        "fundSite": "onshore",
                        "fundName": "統一台灣高息優選月配型基金（新台幣）",
                        "currencyName": "新台幣",
                        "asiFreq": "月配息",
                        "navValue": 23.07,
                        "navTxnDate": "2026/07/31",
                        "changeRatio": 8.25,
                    }
                ]
            }
        assert body and body["fundCode"] == "93110333B"
        return {
            "list": [
                {
                    "infoDate": "20260715",
                    "infoContent": "收益分配公告",
                    "infoUrl": "https://example.test/fund/dividend.pdf",
                }
            ]
        }

    result = MarketDataSearch(fake_fetch).search(
        {
            "holdings": [
                {
                    "symbol": "FUND-R004",
                    "name": "統一台灣高息優選月配型(台幣)",
                    "market": "FUND",
                    "asset_type": "FUND",
                    "currency": "TWD",
                }
            ]
        }
    )

    assert result["ok"] is True
    assert result["queued"] is False
    fund = result["results"][0]
    assert fund["requested_symbol"] == "FUND-R004"
    assert fund["official_code"] == "93110333B"
    assert fund["parameters"]["price"] == 23.07
    assert fund["distribution"]["frequency"] == "monthly"
    assert fund["distribution_events"][0]["evidence_only"] is True
    assert fund["sources"][0]["kind"] == "official-fund-nav"


def test_star_reports_no_distribution_and_uses_daily_negative_cache() -> None:
    calls: list[str] = []

    def fake_fetch(url: str, body: dict | None = None) -> dict:
        calls.append(url)
        if url == FUNDCLEAR_SEARCH_URL:
            return {
                "data": [
                    {
                        "fundCode": "NO-DIST-1",
                        "fundSite": "offshore",
                        "fundName": "測試累積型基金",
                        "currencyName": "美元",
                        "asiFreq": "不配息",
                        "navValue": 10.5,
                        "navTxnDate": "2026/08/03",
                    }
                ]
            }
        return {"list": []}

    search = MarketDataSearch(fake_fetch)
    payload = {
        "holdings": [
            {
                "symbol": "FUND-NO-DIST",
                "name": "測試累積型基金",
                "market": "FUND",
                "asset_type": "FUND",
                "currency": "USD",
            }
        ]
    }
    first = search.search(payload)
    first_call_count = len(calls)
    second = search.search(payload)

    assert first["results"][0]["distribution"]["frequency"] == "none"
    assert first["results"][0]["distribution"]["frequency_label"] == "無配息"
    assert first["results"][0]["cache"]["ttl_seconds"] == 86400
    assert second["results"][0]["cache"]["hit"] is True
    assert second["results"][0]["cache"]["ttl_policy"] == "no-distribution-daily"
    assert len(calls) == first_call_count


def test_star_database_is_owned_and_parameterized_by_local_ai(tmp_path: Path) -> None:
    repository = LocalAiRepository(tmp_path)
    response = {
        "ok": True,
        "searched_at": "2026-08-02T00:00:00+00:00",
        "results": [
            {
                "identity_key": "yahoo:AAPL",
                "requested_symbol": "AAPL",
                "resolved_symbol": "AAPL",
                "market": "US",
                "asset_type": "STOCK",
                "currency": "USD",
                "confidence": 0.9,
                "observed_at": "2026-08-01T20:00:00+00:00",
                "parameters": {"price": 201.5, "change_percent": 1.2},
                "parameter_units": {"price": "currency", "change_percent": "%"},
                "sources": [
                    {
                        "name": "Yahoo Finance",
                        "url": "https://query1.finance.yahoo.com/example",
                    }
                ],
                "distribution_events": [],
            }
        ],
    }
    repository.record_market_search({"holdings": [{"symbol": "AAPL"}]}, response)
    status = repository.database_status()

    assert status["path"].startswith(str(tmp_path.resolve()))
    assert status["tables"]["investment_parameter_definition"] >= 15
    assert status["tables"]["instrument_identity"] == 1
    assert status["tables"]["market_observation"] == 2
    assert "ai-assistant" not in status["path"]


def test_star_database_rejects_blank_events_deduplicates_and_compacts_logs(
    tmp_path: Path,
) -> None:
    repository = LocalAiRepository(tmp_path)
    request = {"holdings": [{"symbol": "FUND-1"}]}

    def response(searched_at: str, observed_at: str) -> dict:
        return {
            "ok": True,
            "searched_at": searched_at,
            "requested_count": 1,
            "updated_count": 1,
            "error_count": 0,
            "large_diagnostic": "x" * 200_000,
            "results": [
                {
                    "identity_key": "fundclear:offshore:FUND-1",
                    "requested_symbol": "FUND-1",
                    "resolved_symbol": "FUND-1",
                    "market": "FUND",
                    "asset_type": "FUND",
                    "currency": "USD",
                    "confidence": 0.9,
                    "observed_at": observed_at,
                    "parameters": {"price": 10.5},
                    "sources": [{"name": "FundClear", "url": "https://example.test/fund"}],
                    "distribution_events": [
                        {
                            "observed_at": observed_at,
                            "source_url": "https://example.test/blank",
                        },
                        {
                            "record_date": "2026-07-15",
                            "observed_at": observed_at,
                            "title": "配息公告",
                            "source_url": "https://example.test/distribution",
                        },
                    ],
                }
            ],
        }

    repository.record_market_search(
        request,
        response("2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
    )
    repository.record_market_search(
        request,
        response("2026-08-03T01:00:00+00:00", "2026-08-03T01:00:00+00:00"),
    )

    with sqlite3.connect(repository.database_path) as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM distribution_event").fetchone()[0]
        log = connection.execute(
            "SELECT occurrence_count, response_json FROM web_search_log"
        ).fetchone()

    assert event_count == 1
    assert log is not None
    assert log[0] == 2
    assert "large_diagnostic" not in log[1]
    assert len(log[1]) < 65536
    assert repository.database_status()["quality"] == {
        "blank_distribution_events": 0,
        "duplicate_distribution_events": 0,
        "search_request_occurrences": 2,
        "search_payload_chars": len(json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + len(log[1]),
        "max_search_response_chars": len(log[1]),
        "search_log_retention_days": 7,
        "search_log_limit": 1000,
    }


def test_star_service_coalesces_repeated_searches(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    calls = 0

    def fake_search(_payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return {
            "ok": True,
            "searched_at": "2026-08-03T00:00:00+00:00",
            "requested_count": 0,
            "updated_count": 0,
            "error_count": 0,
            "results": [],
            "errors": [],
        }

    service.market_data.search = fake_search
    payload = {"holdings": [{"symbol": "AAPL", "market": "US"}]}

    async def run() -> tuple[dict, dict]:
        _, first = await service.handle("local_ai_search_investments", payload)
        _, second = await service.handle("local_ai_search_investments", payload)
        return first, second

    first, second = asyncio.run(run())

    assert calls == 1
    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert service.repository.database_status()["tables"]["web_search_log"] == 1


def test_star_native_language_model_uses_no_external_model(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)

    async def run() -> tuple[dict, dict]:
        _, status = await service.handle("local_ai_status", {})
        _, inference = await service.handle(
            "local_ai_infer",
            {"prompt": "請說明無配息基金如何處理"},
        )
        return status, inference

    status, inference = asyncio.run(run())

    assert status["model"] == "star-native-language-model"
    assert status["model_mode"] == "native-language-model"
    assert status["external_model_used"] is False
    assert inference["ok"] is True
    assert inference["intent"] == "distribution"
    assert inference["third_party_weights_used"] is False
    assert inference["network_used_for_inference"] is False
    assert "無配息" in inference["response"]


def test_star_native_language_model_can_search_public_sources(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    searches = 0

    def fake_search(_payload: dict) -> dict:
        nonlocal searches
        searches += 1
        return {
            "ok": True,
            "searched_at": "2026-08-03T00:00:00+00:00",
            "requested_count": 1,
            "updated_count": 1,
            "error_count": 0,
            "results": [{"identity_key": "yahoo:AAPL", "parameters": {"price": 200}}],
            "errors": [],
        }

    service.market_data.search = fake_search

    async def run() -> dict:
        _, inference = await service.handle(
            "local_ai_infer",
            {
                "prompt": "查詢 AAPL 報價",
                "holdings": [{"symbol": "AAPL", "market": "US"}],
            },
        )
        return inference

    inference = asyncio.run(run())

    assert searches == 1
    assert inference["network_used_for_inference"] is True
    assert inference["network_scope"] == "public-investment-sources-read-only"
    assert inference["third_party_weights_used"] is False
    assert service.repository.database_status()["tables"]["web_search_log"] == 1


def test_star_uses_external_ai_only_for_explicit_unresolved_collaboration(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)
    collaborations = 0

    def unresolved(_payload: dict) -> dict:
        return {
            "ok": False,
            "searched_at": "2026-08-03T00:00:00+00:00",
            "requested_count": 1,
            "updated_count": 0,
            "error_count": 1,
            "results": [],
            "errors": [{"symbol": "UNKNOWN", "message": "unresolved"}],
        }

    def collaborate(_errors: list[dict]) -> dict:
        nonlocal collaborations
        collaborations += 1
        return {"ok": True, "queued": False, "uses_api": False}

    service.market_data.search = unresolved
    service.external_research.search = collaborate
    base = {"holdings": [{"symbol": "UNKNOWN", "market": "US"}]}

    async def run() -> tuple[dict, dict]:
        _, local_only = await service.handle("local_ai_search_investments", base)
        _, explicit = await service.handle(
            "local_ai_search_investments",
            {**base, "allow_external_fallback": True},
        )
        return local_only, explicit

    local_only, explicit = asyncio.run(run())

    assert collaborations == 1
    assert "external_research" not in local_only
    assert explicit["external_research"]["used_by_native_model"] is False
    assert explicit["external_research"]["role"] == "supplemental-collaboration-only"


def test_star_analysis_uses_its_own_parameters() -> None:
    result = analyze_investments(
        {
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 1,
                    "current_value_twd": 90,
                    "currency": "USD",
                    "market_data_updated_at": "2026-08-02T00:00:00+00:00",
                },
                {
                    "symbol": "0050",
                    "quantity": 1,
                    "current_value_twd": 10,
                    "currency": "TWD",
                    "market_data_updated_at": "2026-08-02T00:00:00+00:00",
                },
            ],
            "analysis_parameters": {"position_concentration_percent": 80},
        }
    )
    assert result["owner"] == "星澄"
    assert result["analysis_parameters"]["position_concentration_percent"] == 80
    assert result["risk_warnings"][0]["code"] == "POSITION_CONCENTRATION"


def test_external_ai_research_never_uses_api_or_queues(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GPTBRIDGE_EXTERNAL_AI_WS_URL", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = ExternalBrowserResearch().search(
        [{"symbol": "UNKNOWN", "name": "未知基金", "message": "unresolved"}]
    )
    assert result["ok"] is False
    assert result["queued"] is False
    assert result["uses_api"] is False
    assert result["error_code"] == "EXTERNAL_AI_NOT_CONNECTED"


def test_manifests_forbid_external_ai_api() -> None:
    local_manifest = json.loads(
        (ROOT / "platform_tools" / "local-ai" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    external_manifest = json.loads(
        (ROOT / "platform_tools" / "ai-collaboration" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    connection = local_manifest["capabilities"]["ai-connections"]
    assert connection["api"] is False
    assert connection["queue_when_offline"] is False
    assert connection["peers"] == ["ai-collaboration"]
    assert connection["external_model_inference"] is False
    assert connection["external_collaboration"] == "explicit-need-only"
    assert external_manifest["capabilities"]["external-ai"]["api"] is False
    assert external_manifest["capabilities"]["external-ai"]["transport"] == "browser-session"


def test_star_upgrade_evaluation_locks_version_and_uses_hot_update(tmp_path: Path) -> None:
    repository = LocalAiRepository(tmp_path)
    evaluation = evaluate_star_upgrade(
        version="1.0.0",
        tool_root=tmp_path,
        database=repository.database_status(),
        external_research_configured=True,
    )

    assert evaluation["ok"] is True
    assert evaluation["schema"] == "star-upgrade-evaluation/v1"
    assert evaluation["name"] == "星澄"
    assert evaluation["version"] == "1.0"
    assert evaluation["checks"]["tool_database_isolated"] is True
    assert evaluation["metrics"]["investment_parameter_count"] >= 15
    assert evaluation["upgrade_policy"] == {
        "mode": "direct-source-hot-update",
        "repackage_exe_for_source_changes": False,
        "version_change_allowed": False,
        "restart_scope": "affected-tool-only",
    }
    assert evaluation["checks"]["star_native_language_model_enabled"] is True
    assert evaluation["checks"]["external_model_disabled"] is True
    assert not any(
        item["code"] == "CONFIGURE_LOCAL_MODEL"
        for item in evaluation["recommendations"]
    )
