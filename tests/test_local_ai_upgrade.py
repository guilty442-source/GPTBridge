from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

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

from ai_nexus import investment_manager_core
from ai_nexus.investment_local_ai_upgrade import LocalExplanationEngine
from ai_nexus.local_risk_ai import engine as local_risk_ai_engine


def _offline_analysis() -> dict[str, Any]:
    return local_risk_ai_engine.analyze_holdings(
        [
            investment_manager_core.Holding(
                symbol="AAPL",
                market="US",
                asset_type="STOCK",
                quantity=1,
                average_cost=100,
                currency="USD",
            )
        ],
        live_quotes=False,
        now=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
    )


def _installed_local_engine(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
) -> tuple[LocalExplanationEngine, dict[str, Any]]:
    local_engine = LocalExplanationEngine(model="tiny-local:test")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(local_engine, "_local_server_available", lambda: True)
    monkeypatch.setattr(
        local_engine,
        "_installed_models",
        lambda: [{"name": "tiny-local:test", "size": 500_000_000}],
    )

    def fake_post(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        captured.update({"url": url, "payload": payload, "timeout": timeout})
        return {"response": json.dumps(response, ensure_ascii=False)}

    monkeypatch.setattr(local_engine, "_post_json", fake_post)
    return local_engine, captured


def test_golden_missing_research_data_is_unavailable_not_neutral() -> None:
    result = _offline_analysis()
    assessment = result["local_ai_assessment"]["assessments"][0]
    components = {
        item["component_key"]: item for item in assessment["score_components"]
    }

    assert result["data_mode"] == "offline"
    assert result["execution_mode"]["network_access"] == "none"
    assert result["execution_mode"]["external_ai_enabled"] is False
    assert result["simulation_only"] is True
    assert result["human_approval_required"] is True
    assert assessment["score_type"] == "heuristic_risk_proxy"
    assert assessment["score_label"] == "啟發式風險代理分數"
    assert assessment["rating"] == "風險代理"
    assert assessment["rating_label"].startswith("本地風險訊號")
    assert "偏多" not in assessment["rating_label"]
    assert result["local_ai_assessment"]["portfolio_rating"]["rating"] == "風險代理"

    for key in (
        "fundamentals",
        "growth",
        "profitability",
        "financial_safety",
        "positioning",
        "valuation",
    ):
        assert components[key]["status"] == "unavailable"
        assert components[key]["score"] is None
        assert components[key]["coverage"] == 0
        assert components[key]["evidence_ids"] == []
        assert components[key]["included_in_score"] is False

    assert components["risk"]["status"] == "available"
    assert components["risk"]["scoring_basis"] == "heuristic_risk_proxy"
    assert components["risk"]["evidence_ids"]
    assert assessment["score_coverage_percent"] < 50
    assert result["local_ai_assessment"]["coverage_warning"]


def test_explicit_research_contract_requires_evidence_before_scoring() -> None:
    base_holding = {
        "symbol": "AAPL",
        "market": "US",
        "asset_type": "STOCK",
        "quantity": 1,
        "average_cost": 100,
        "currency": "USD",
    }
    partial = local_risk_ai_engine.analyze_state(
        {
            "holdings": [
                {
                    **base_holding,
                    "research_inputs": {
                        "fundamentals": {
                            "normalized_score": 80,
                            "source": "local-test",
                            "as_of": "2026-07-28",
                        }
                    },
                }
            ]
        },
        live_quotes=False,
    )
    missing_provenance = local_risk_ai_engine.analyze_state(
        {
            "holdings": [
                {
                    **base_holding,
                    "research_inputs": {
                        "fundamentals": {
                            "normalized_score": 80,
                            "free_cash_flow": 100,
                        }
                    },
                }
            ]
        },
        live_quotes=False,
    )
    scored = local_risk_ai_engine.analyze_state(
        {
            "holdings": [
                {
                    **base_holding,
                    "research_inputs": {
                        "fundamentals": {
                            "normalized_score": 80,
                            "free_cash_flow": 100,
                            "source": "local-test",
                            "as_of": "2026-07-28",
                        }
                    },
                }
            ]
        },
        live_quotes=False,
    )

    partial_component = partial["local_ai_assessment"]["assessments"][0][
        "score_components"
    ][0]
    missing_provenance_component = missing_provenance["local_ai_assessment"][
        "assessments"
    ][0]["score_components"][0]
    scored_assessment = scored["local_ai_assessment"]["assessments"][0]
    scored_component = scored_assessment["score_components"][0]

    assert partial_component["status"] == "partial"
    assert partial_component["score"] is None
    assert partial_component["included_in_score"] is False
    assert missing_provenance_component["score"] is None
    assert missing_provenance_component["included_in_score"] is False
    assert "fundamentals.source" in missing_provenance_component["required_data"]
    assert "fundamentals.as_of" in missing_provenance_component["required_data"]
    assert scored_component["status"] == "partial"
    assert scored_component["score"] == scored_component["max_score"] * 0.8
    assert scored_component["evidence_ids"] == [
        "research:aapl:fundamentals:free_cash_flow"
    ]
    assert scored_assessment["score_type"] == "evidence_weighted_local_assessment"
    assert scored_assessment["rating"] != "風險代理"
    assert scored_assessment["evidence"][0]["source"] == "local_portfolio"
    assert any(
        item["id"] == "research:aapl:fundamentals:free_cash_flow"
        and item["source"] == "local-test"
        for item in scored_assessment["evidence"]
    )


def test_public_quote_mode_is_explicit_and_preserves_local_boundaries() -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def quote_function(
        holding: investment_manager_core.Holding,
        _providers: dict[str, Any],
        _order: list[str],
        checked_at: datetime,
    ) -> tuple[
        investment_manager_core.Quote,
        list[investment_manager_core.QuoteAttempt],
        list[investment_manager_core.Quote],
    ]:
        quote = investment_manager_core.Quote(
            symbol=holding.symbol,
            requested_symbol=holding.symbol,
            provider="public-test",
            price=102,
            currency="USD",
            change_percent=2,
            as_of=checked_at.isoformat(),
        )
        return (
            quote,
            [investment_manager_core.QuoteAttempt("public-test", True, "ok")],
            [quote],
        )

    result = local_risk_ai_engine.analyze_holdings(
        [
            investment_manager_core.Holding(
                symbol="AAPL",
                market="US",
                quantity=1,
                average_cost=100,
                currency="USD",
            )
        ],
        live_quotes=True,
        quote_function=quote_function,
        now=now,
    )
    assessment = result["local_ai_assessment"]["assessments"][0]
    technical = next(
        item
        for item in assessment["score_components"]
        if item["component_key"] == "technical"
    )

    assert result["data_mode"] == "local_with_public_data"
    assert result["execution_mode"]["public_market_data_enabled"] is True
    assert result["execution_mode"]["external_ai_enabled"] is False
    assert result["execution_mode"]["network_access"] == "public_market_data_only"
    assert technical["status"] == "partial"
    assert technical["evidence_ids"] == ["quote:aapl:change_percent"]


def test_analytics_context_is_bounded_cited_and_never_changes_score() -> None:
    base_state = {
        "holdings": [
            {
                "symbol": "AAPL",
                "market": "US",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
        ]
    }
    context = {
        "risk": {
            "portfolio_var": 12.5,
            "source": "local-risk-store",
            "as_of": "2026-07-28",
        },
        "events": [
            {
                "event_id": "event-aapl",
                "symbol": "AAPL",
                "title": "財報事件",
                "occurred_at": "2026-07-28",
            },
            {
                "event_id": "event-msft",
                "symbol": "MSFT",
                "title": "不相關事件",
            },
        ],
        "decision_journal": [
            {
                "decision_id": "decision-aapl",
                "symbol": "AAPL",
                "thesis": "等待新證據後再評估",
            }
        ],
        "ledger_summary": {"realized_pnl": 50},
        "policy": {
            "max_position_percent": 20,
            "api_key": "must-not-leak",
        },
        "unapproved_section": {"invented_signal": 999},
    }
    without_context = local_risk_ai_engine.analyze_state(
        base_state,
        live_quotes=False,
    )
    with_context = local_risk_ai_engine.analyze_state(
        {**base_state, "analytics_context": context},
        live_quotes=False,
    )

    evidence = with_context["analytics_evidence"]
    evidence_ids = {item["id"] for item in evidence}
    evidence_values = {str(item["value"]) for item in evidence}

    assert with_context["analytics_context"]["provided"] is True
    assert with_context["analytics_context"]["used_for_scoring"] is False
    assert with_context["analytics_context"]["used_for_explanation"] is True
    assert set(with_context["analytics_context"]["accepted_sections"]) == {
        "risk",
        "events",
        "decision_journal",
        "ledger_summary",
        "policy",
    }
    assert len(evidence) <= 80
    assert len(evidence_ids) == len(evidence)
    assert "analytics:risk:portfolio_var" in evidence_ids
    assert "analytics:events:event-aapl:title" in evidence_ids
    assert "analytics:decision_journal:decision-aapl:thesis" in evidence_ids
    assert "analytics:policy:max_position_percent" in evidence_ids
    assert not any("event-msft" in evidence_id for evidence_id in evidence_ids)
    assert not any("unapproved_section" in evidence_id for evidence_id in evidence_ids)
    assert "must-not-leak" not in evidence_values
    assert (
        with_context["local_ai_assessment"]["portfolio_score"]
        == without_context["local_ai_assessment"]["portfolio_score"]
    )
    assert set(
        with_context["local_ai_assessment"]["data_availability"][
            "analytics_context_evidence_ids"
        ]
    ) == evidence_ids


@pytest.mark.skip(reason="Direct model access moved to the independent 星澄 tool")
def test_schema_bound_local_model_can_explain_without_exact_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "summary": "風險仍需監測；資料信心中，請人工確認。",
        "evidence_ids": [
            "portfolio:warning_count",
            "assessment:data_confidence",
        ],
        "counterfactuals": [
            {
                "condition": "若風險警示或資料信心改變",
                "effect": "重新執行本地規則並由人工確認",
                "evidence_ids": [
                    "portfolio:warning_count",
                    "assessment:data_confidence",
                ],
            }
        ],
        "abstain": False,
        "abstention_reason": "",
    }
    local_engine, captured = _installed_local_engine(monkeypatch, response)

    explanation = local_engine.explain(
        _offline_analysis(),
        {"evaluated_count": 0},
    )

    assert explanation["mode"] == "hybrid"
    assert explanation["structured"] == response
    assert explanation["facts_locked"] is True
    assert explanation["counterfactuals"] == response["counterfactuals"]
    assert "反證條件" in explanation["text"]
    assert captured["url"].endswith("/api/generate")
    assert captured["payload"]["format"]["additionalProperties"] is False
    assert captured["payload"]["model"] == "tiny-local:test"
    assert captured["payload"]["options"]["temperature"] == 0.1
    assert "api/pull" not in captured["url"]


@pytest.mark.skip(reason="Direct model access moved to the independent 星澄 tool")
def test_local_model_can_cite_decision_context_for_counterfactual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = local_risk_ai_engine.analyze_state(
        {
            "holdings": [
                {
                    "symbol": "AAPL",
                    "market": "US",
                    "quantity": 1,
                    "average_cost": 100,
                    "currency": "USD",
                }
            ],
            "analytics_context": {
                "policy": {"review_when_policy_changes": True},
                "decision_journal": [
                    {
                        "decision_id": "decision-aapl",
                        "symbol": "AAPL",
                        "thesis": "等待新證據後再評估",
                    }
                ],
            },
        },
        live_quotes=False,
    )
    policy_id = "analytics:policy:review_when_policy_changes"
    journal_id = "analytics:decision_journal:decision-aapl:thesis"
    response = {
        "summary": "風險仍需監測；資料信心中，請人工確認。",
        "evidence_ids": [journal_id],
        "counterfactuals": [
            {
                "condition": "若政策或決策日誌中的條件改變",
                "effect": "重新執行本地規則並由人工確認",
                "evidence_ids": [policy_id, journal_id],
            }
        ],
        "abstain": False,
        "abstention_reason": "",
    }
    local_engine, _captured = _installed_local_engine(monkeypatch, response)

    explanation = local_engine.explain(analysis, {"evaluated_count": 0})

    assert explanation["mode"] == "hybrid"
    assert explanation["counterfactuals"][0]["evidence_ids"] == [
        policy_id,
        journal_id,
    ]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            {
                "summary": "風險將改善 87%；資料信心中，請人工確認。",
                "evidence_ids": ["portfolio:warning_count"],
                "counterfactuals": [],
                "abstain": False,
                "abstention_reason": "",
            },
            "number_not_supported_by_evidence",
        ),
        (
            {
                "summary": "風險仍需監測；資料信心中，請人工確認。",
                "evidence_ids": ["invented:evidence"],
                "counterfactuals": [],
                "abstain": False,
                "abstention_reason": "",
            },
            "unknown_evidence_id",
        ),
    ],
)
@pytest.mark.skip(reason="Direct model access moved to the independent 星澄 tool")
def test_adversarial_model_output_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
    reason: str,
) -> None:
    local_engine, _captured = _installed_local_engine(monkeypatch, response)

    explanation = local_engine.explain(
        _offline_analysis(),
        {"evaluated_count": 0},
    )

    assert explanation["mode"] == "deterministic"
    assert explanation["model_output_rejected"] is True
    assert explanation["model_rejection_reason"] == reason
    assert explanation["facts_locked"] is True


@pytest.mark.skip(reason="Direct model access moved to the independent 星澄 tool")
def test_local_model_can_abstain_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "summary": "",
        "evidence_ids": [],
        "counterfactuals": [],
        "abstain": True,
        "abstention_reason": "資料證據不足，停止模型解釋並交由人工確認。",
    }
    local_engine, _captured = _installed_local_engine(monkeypatch, response)

    explanation = local_engine.explain(
        _offline_analysis(),
        {"evaluated_count": 0},
    )

    assert explanation["mode"] == "deterministic"
    assert explanation["model_abstained"] is True
    assert explanation["model_output_rejected"] is False
    assert explanation["abstention_reason"] == response["abstention_reason"]
    assert "風險摘要" in explanation["text"]


@pytest.mark.skip(reason="Model selection is owned by the independent 星澄 tool")
def test_model_profile_uses_installed_only_conservative_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GPTBRIDGE_LOCAL_LLM_PROFILE", "compact")
    local_engine = LocalExplanationEngine(model="not-installed:model")
    profile = local_engine._model_profile()
    selection = local_engine._select_installed_model(
        [
            {"name": "large-local:7b", "size": 5_000_000_000},
            {"name": "small-local:1b", "size": 800_000_000},
            {"name": "embedding-local", "size": 100_000_000},
        ],
        profile,
    )

    assert profile["id"] == "compact"
    assert profile["no_download"] is True
    assert profile["selection_policy"] == "installed_models_only"
    assert selection["model"] == "small-local:1b"
    assert selection["requested_model_available"] is False
    assert selection["download_attempted"] is False


@pytest.mark.skip(reason="Model endpoint validation is owned by the independent 星澄 tool")
def test_remote_local_model_endpoint_is_blocked_without_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_engine = LocalExplanationEngine(endpoint="https://example.com:11434")
    monkeypatch.setattr(
        local_engine,
        "_local_server_available",
        lambda: pytest.fail("remote endpoint must be blocked before socket access"),
    )

    explanation = local_engine.explain(
        _offline_analysis(),
        {"evaluated_count": 0},
    )

    assert explanation["mode"] == "deterministic"
    assert explanation["model_unavailable_reason"] == "non_local_endpoint_blocked"
