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

def test_star_self_upgrade_can_author_unified_diff_for_existing_file() -> None:
    result = StarCodingExpert().process(
        {
            "prompt": "更新既有 TypeScript 模組",
            "original_source": "export function total() {\n  return 0;\n}\n",
            "code_spec": {
                "language": "typescript",
                "name": "total",
                "parameters": ["values"],
                "operation": "sum",
                "target_path": "src/backend/services/xingcheng/application/total.ts",
            },
        },
        "self_upgrade",
    )

    proposal = result["upgrade_proposal"]
    assert result["ok"] is True
    assert proposal["proposal_ready"] is True
    assert proposal["change_type"] == "modify"
    assert proposal["target"]["new_file_only"] is False
    assert "--- a/src/backend/services/xingcheng/application/total.ts" in proposal[
        "unified_diff"
    ]
    assert "+++ b/src/backend/services/xingcheng/application/total.ts" in proposal[
        "unified_diff"
    ]


def test_project_source_scope_allows_tools_and_denies_governance_rule() -> None:
    expert = StarCodingExpert()
    allowed = expert.process(
        {
            "prompt": "update project source",
            "code_spec": {
                "language": "typescript",
                "name": "healthCheck",
                "return_expression": "true",
                "target_path": "main-system/src/healthCheck.ts",
            },
        },
        "self_upgrade",
    )
    denied = expert.process(
        {
            "prompt": "update governance",
            "code_spec": {
                "language": "python",
                "name": "governance_change",
                "return_expression": "True",
                "target_path": "governance_rule/change.py",
            },
        },
        "self_upgrade",
    )

    assert allowed["upgrade_proposal"]["proposal_ready"] is True
    assert allowed["upgrade_proposal"]["target"]["within_project_source"] is True
    assert denied["upgrade_proposal"]["proposal_ready"] is False
    assert denied["upgrade_proposal"]["target"]["within_project_source"] is False
    assert denied["upgrade_proposal"]["target"]["governance_rule_excluded"] is True


def test_star_sanitizes_types_and_rejects_stacked_sql() -> None:
    expert = StarCodingExpert()
    sanitized = expert.process(
        {
            "prompt": "建立 TypeScript 函式",
            "code_spec": {
                "language": "typescript",
                "name": "safeType",
                "parameters": ["value"],
                "parameter_types": {"value": "number); eval('unsafe') //"},
                "return_type": "number { malicious(): void }",
                "return_expression": "value",
            },
        },
        "coding",
    )
    stacked_sql = expert.process(
        {
            "prompt": "分析多語句 SQL",
            "source_code": "SELECT 1; SELECT 2;",
            "code_spec": {"action": "analyze", "language": "sql"},
        },
        "coding",
    )

    assert sanitized["ok"] is True
    assert "value: unknown" in sanitized["source"]
    assert "): unknown {" in sanitized["source"]
    assert "eval" not in sanitized["source"]
    assert stacked_sql["ok"] is False
    assert "exactly one SQL statement is required" in stacked_sql["validation"][
        "errors"
    ]


def test_star_routes_programming_languages_and_reports_capabilities(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _, inference = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {"prompt": "請用 TypeScript 寫一個計算平均值的函式"},
        )
    )
    _, status = asyncio.run(service.handle("xingcheng_status", {}))

    assert inference["intent"] == "coding"
    assert inference["model_role"] == "coding-specialist"
    assert inference["coding_result"]["language"] == "typescript"
    assert inference["coding_result"]["ok"] is True
    assert inference["autonomous_agent"]["enabled"] is True
    assert inference["autonomous_agent"]["star_native_model_included"] is False
    assert inference["autonomous_agent"]["project_scope"] == (
        "all-project-source-excluding-governance-rule"
    )
    assert status["coding"]["languages"] == [
        "javascript",
        "json",
        "python",
        "sql",
        "typescript",
    ]
    assert status["coding"]["sql_policy"] == "parameterized-read-only-select"
    assert status["coding"]["unified_diff_proposals"] is True
    assert status["coding"]["direct_source_write"] is False
    assert status["autonomous_agent"]["enabled"] is True


def test_star_authors_bounded_self_upgrade_proposal(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請自我升級並建立新的程式模組",
                "code_spec": {
                    "language": "python",
                    "name": "new_capability",
                    "parameters": ["payload"],
                    "return_expression": "{'ok': True}",
                    "target_path": "src/backend/services/xingcheng/application/generated_extension.py",
                },
            },
        )
    )

    proposal = result["coding_result"]["upgrade_proposal"]
    assert result["model_role"] == "coding-specialist"
    assert proposal["self_authored"] is True
    assert proposal["proposal_ready"] is True
    assert proposal["source_write_performed"] is False
    assert proposal["publish_authority"] == "governance-versioned-release-only"
    assert proposal["schema"] == "star-self-upgrade-proposal/v1"
    assert proposal["persistence"]["status"] == "proposed"
    assert proposal["persistence"]["owner_model_id"] == "star-coding-native-model"
    assert service.repositories[
        service.models.CODING.model_id
    ].database_status()["tables"]["code_upgrade_proposal"] == 1
    assert result["instruction_execution"]["status"] == "input-required"
    assert "program-synthesis" in {
        item["module_id"] for item in result["module_execution"]["modules"]
    }


def test_star_runs_autonomous_bounded_self_maintenance(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    asyncio.run(service.start())

    maintenance = service.runtime_health()["self_maintenance"]
    assert maintenance["ok"] is True
    assert maintenance["source_code_modified"] is False
    assert set(maintenance["model_reports"]) == {
        profile.model_id for profile in service.models.profiles
    }
    assert all(
        repository.database_status()["tables"]["language_model_maintenance"] == 1
        for repository in service.repositories.values()
    )


def test_model_routing_assigns_investment_mathematical_and_coding_work() -> None:
    registry = StarModelRegistry()

    assert registry.for_command("xingcheng_infer").role == "daily-primary"
    assert registry.for_command("xingcheng_infer", intent="risk").role == "investment-specialist"
    assert registry.for_command("xingcheng_infer", intent="calculation").role == "mathematical-reasoning-specialist"
    assert registry.for_command("xingcheng_infer", intent="reasoning").role == "mathematical-reasoning-specialist"
    assert registry.for_command("xingcheng_infer", intent="coding").role == "coding-specialist"
    assert registry.for_command("xingcheng_infer", intent="self_upgrade").role == "coding-specialist"
    assert registry.for_command("xingcheng_analyze_investments").role == "investment-specialist"
    assert registry.for_command("xingcheng_search_investments").role == "daily-primary"
    assert registry.for_command("xingcheng_manage_investment_accounting").role == "daily-primary"
    assert registry.INVESTMENT.network_policy == "disabled"
    assert registry.INVESTMENT.external_collaboration == "disabled"
    assert registry.MATHEMATICAL.network_policy == "disabled"
    assert registry.MATHEMATICAL.external_collaboration == "disabled"
    assert registry.CODING.network_policy == "disabled"
    assert registry.CODING.external_collaboration == "disabled"
    assert all(item["selection_mode"] == "automatic" for item in registry.catalog())
    assert all(item["direct_selection"] is False for item in registry.catalog())


def test_star_autonomously_approves_only_safe_accounting_differences() -> None:
    approved = coordinate_investment_accounting(
        {
            "autonomous": True,
            "reconciliation": {
                "differences": [
                    {
                        "symbol": "2330",
                        "suggestion": {
                            "symbol": "2330",
                            "side": "BUY",
                            "quantity": 2,
                            "price": 1000,
                            "currency": "TWD",
                        },
                    }
                ]
            },
        }
    )
    assert approved["accounting_owner"] == "星澄"
    assert approved["apply_reconciliation"] is True
    assert approved["database_access"] is False

    rejected = coordinate_investment_accounting(
        {
            "autonomous": True,
            "reconciliation": {
                "differences": [
                    {
                        "symbol": "2330",
                        "suggestion": {
                            "symbol": "2330",
                            "side": "BUY",
                            "quantity": 2,
                            "price": 0,
                        },
                    }
                ]
            },
        }
    )
    assert rejected["decision"] == "manual_review"
    assert rejected["apply_reconciliation"] is False


def test_main_model_automatically_arranges_multi_specialist_tasks(tmp_path: Path) -> None:
    service = _make_transformer_service(tmp_path)

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {"prompt": "請分析投資風險，再計算並整理資料"},
        )
    )

    arrangement = result["task_arrangement"]
    assert arrangement["mode"] == "traditional-chinese-first-governed-workflow"
    assert arrangement["task_allocation_model"] == "qwen3.8:27b-q4_K_M"
    assert arrangement["integration_model"] == "qwen3.8:27b-q4_K_M"
    assert arrangement["manual_assignment_allowed"] is False
    assert arrangement["star_native_model_included"] is False
    assert arrangement["external_ai_used"] is False
    assert {
        task["assigned_model"] for task in arrangement["tasks"]
    } == {
        "ibm/granite4.2:30b-q4_K_M",
        "deepseek-r1:14b",
    }
    assert all(task["star_native_model_included"] is False for task in arrangement["tasks"])


def test_external_ai_request_remains_disabled(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請用 AI 協作做高階搜尋、長文、深度推理、社群與潮流工作",
                "use_external_collaboration": True,
            },
        )
    )

    plan = result["external_collaboration_plan"]
    assert plan["enabled"] is False
    assert plan["policy"] == "local-ollama-only"
    assert plan["external_ai_used"] is False
    assert plan["tasks"] == []


def test_main_model_is_the_only_coordinator_and_external_collaborator() -> None:
    registry = StarModelRegistry()

    assert registry.primary.role == "daily-primary"
    assert registry.primary.external_collaboration == "disabled"
    assert registry.INVESTMENT.external_collaboration == "disabled"
    assert registry.MATHEMATICAL.external_collaboration == "disabled"
    assert registry.CODING.external_collaboration == "disabled"


def test_each_model_uses_a_distinct_database(tmp_path: Path) -> None:
    paths = {
        LocalAiRepository(tmp_path, database_scope=scope).database_path
        for scope in ("main", "investment", "mathematical", "coding")
    }
    assert paths == {
        tmp_path / "xingcheng" / "runtime" / "state" / "models" / "main.sqlite3",
        tmp_path / "xingcheng" / "runtime" / "state" / "models" / "investment.sqlite3",
        tmp_path / "xingcheng" / "runtime" / "state" / "models" / "mathematical.sqlite3",
        tmp_path / "xingcheng" / "runtime" / "state" / "models" / "coding.sqlite3",
    }


def test_star_brokers_memory_as_copies_between_isolated_databases(
    tmp_path: Path,
) -> None:
    registry = StarModelRegistry()
    repositories = {
        profile.model_id: LocalAiRepository(
            tmp_path, database_scope=profile.database_scope
        )
        for profile in registry.profiles
    }
    broker = StarMemoryBroker(repositories, registry)

    accepted = broker.accept_external_candidates(
        [
            {
                "candidate_id": "candidate-1",
                "kind": "search",
                "title": "官方資料線索",
                "content": "配息頻率仍待官方來源確認",
                "source_agent_id": "gemini",
                "status": "candidate",
            }
        ],
        business_scope="investment",
        task_type="search",
    )

    assert len(accepted) == 2
    assert {item["owner_model_id"] for item in accepted} == {
        registry.MAIN.model_id,
        registry.INVESTMENT.model_id,
    }
    assert all(item["database_shared"] is False for item in accepted)
    assert all(item["review_status"] == "pending-review" for item in accepted)
    assert repositories[registry.MAIN.model_id].memory_context("investment") == []
    assert repositories[registry.INVESTMENT.model_id].memory_context("investment") == []
    assert repositories[registry.MAIN.model_id].memory_context(
        "investment", include_pending=True
    )
    for item in accepted:
        repositories[item["owner_model_id"]].review_memory(
            item["memory_id"],
            action="approve",
            reviewer="test-owner",
            reason="verified source",
        )
    assert repositories[registry.MAIN.model_id].memory_context("investment")
    assert repositories[registry.INVESTMENT.model_id].memory_context("investment")
    assert repositories[registry.MATHEMATICAL.model_id].memory_context("investment") == []
    assert len({repo.database_path for repo in repositories.values()}) == 4


def test_only_star_main_can_broker_model_memory(tmp_path: Path) -> None:
    repository = LocalAiRepository(tmp_path, database_scope="mathematical")

    with pytest.raises(PermissionError, match="MODEL_MEMORY_BROKER_DENIED"):
        repository.store_brokered_memory(
            kind="reasoning",
            title="越權記憶",
            content="不允許外部模型直接寫入",
            business_scope="general",
            source_type="external-ai-candidate",
            source_id="candidate-2",
            source_model_id="deepseek",
            broker_model_id="deepseek",
            confidence=0.5,
        )

def test_model_database_rejects_cross_model_records(tmp_path: Path) -> None:
    repository = LocalAiRepository(tmp_path, database_scope="investment")

    with pytest.raises(PermissionError, match="MODEL_DATABASE_ISOLATION_DENIED"):
        repository.record("star-main-native-model", {}, {})
    with pytest.raises(PermissionError, match="MODEL_DATABASE_ISOLATION_DENIED"):
        repository.record_market_search({}, {})

    status = repository.database_status()
    assert status["owner_model_id"] == "star-investment-native-model"
    assert status["database_scope"] == "investment"
    assert status["isolation_enforced"] is True


def test_main_model_coordinates_and_isolates_specialist_records(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    async def exercise() -> list[dict[str, object]]:
        outputs = []
        for payload in (
            {"prompt": "日常說明"},
            {
                "prompt": "請分析這項投資",
                "holdings": [{"symbol": "TEST", "quantity": 1, "current_value_twd": 100}],
            },
            {"prompt": "請進行邏輯推理"},
        ):
            _, result = await service.handle(
                "xingcheng_infer", {**payload, "allow_network": True}
            )
            outputs.append(result)
        return outputs

    daily, investment, mathematical = asyncio.run(exercise())

    assert daily["model_role"] == "daily-primary"
    assert investment["model_role"] == "investment-specialist"
    assert mathematical["model_role"] == "mathematical-reasoning-specialist"
    assert investment["network_scope"] == "disabled-by-request"
    assert mathematical["network_scope"] == "disabled-by-request"
    assert all(item["coordinator_model"] == "star-main-native-model" for item in (daily, investment, mathematical))
    assert daily["delegated"] is False
    assert investment["delegated"] is True
    assert mathematical["delegated"] is True

    assert service.repositories[service.models.MAIN.model_id].database_path != service.repositories[service.models.INVESTMENT.model_id].database_path
    assert service.repositories[service.models.MATHEMATICAL.model_id].database_path != service.repositories[service.models.INVESTMENT.model_id].database_path

    investment_status = service.repositories[
        service.models.INVESTMENT.model_id
    ].database_status()
    main_status = service.repositories[service.models.MAIN.model_id].database_status()
    math_status = service.repositories[
        service.models.MATHEMATICAL.model_id
    ].database_status()
    assert investment_status["tables"]["investment_parameter_definition"] == 15
    assert investment_status["tables"]["investment_model_definition"] == 8
    assert main_status["tables"]["investment_parameter_definition"] == 0
    assert main_status["tables"]["investment_model_definition"] == 0
    assert math_status["tables"]["investment_parameter_definition"] == 0
    assert math_status["tables"]["investment_model_definition"] == 0
    assert math_status["tables"]["mathematical_capability_definition"] == 16
    assert main_status["tables"]["mathematical_capability_definition"] == 0
    assert investment_status["tables"]["mathematical_capability_definition"] == 0


def test_main_model_handles_specialist_fallback_and_rejects_manual_selection(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)

    _, fallback = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "請計算這個結果"})
    )
    _, denied = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請分析投資",
                "model_id": "star-investment-native-model",
            },
        )
    )

    assert fallback["model_role"] == "daily-primary"
    assert fallback["specialist_fallback"]["used"] is True
    assert fallback["specialist_fallback"]["attempted_model"] == (
        "star-mathematical-native-model"
    )
    assert fallback["instruction_execution"]["status"] == "input-required"
    assert denied["ok"] is False
    assert denied["error_code"] == "DIRECT_MODEL_ACCESS_DENIED"


def test_mathematical_expert_executes_calculation_statistics_and_organization(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)

    async def exercise() -> tuple[dict[str, object], ...]:
        results = []
        for payload in (
            {"prompt": "請計算 2 + 3 * 4"},
            {"prompt": "請做統計", "numbers": [1, 2, 3, 4]},
            {
                "prompt": "請整理資料",
                "records": [{"type": "A", "value": 1}, {"type": "B", "value": None}],
                "group_by": "type",
            },
        ):
            _, result = await service.handle("xingcheng_infer", payload)
            results.append(result)
        return tuple(results)

    calculation, statistical, organization = asyncio.run(exercise())
    assert calculation["mathematical_result"]["calculation"]["value"] == 14
    assert statistical["mathematical_result"]["statistics"]["mean"] == 2.5
    assert organization["mathematical_result"]["data_organization"]["row_count"] == 2
    assert all(
        result["instruction_execution"]["status"] == "completed"
        for result in (calculation, statistical, organization)
    )
