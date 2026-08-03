from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from local_ai.domain.model_registry import StarModelRegistry
from local_ai.domain.module_registry import StarModuleRegistry
from local_ai.integration.memory_broker import StarMemoryBroker
from local_ai.infrastructure.repository import LocalAiRepository
from local_ai.infrastructure.ollama_model_repository import OllamaModelRepository
from local_ai.infrastructure.model_engines import StarModelEngines
from local_ai.infrastructure.generative_language_model import (
    StarAutoregressiveLanguageModel,
)
from local_ai.infrastructure.native_model import StarNativeLanguageModel
from local_ai.infrastructure import repository as repository_module
from local_ai.application.service import LocalAiService
from local_ai.infrastructure.market_data import (
    MarketDataSearch,
    _fund_query_terms,
    _yahoo_symbol_candidates,
    market_source_catalog,
    recognize_holding_identity,
)
from local_ai.application.investment_accounting import coordinate_investment_accounting
from local_ai.application.investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from local_ai.application.coding_expert import StarCodingExpert


def test_star_has_four_governed_model_roles() -> None:
    registry = StarModelRegistry()
    catalog = registry.catalog()

    assert [item["role"] for item in catalog] == [
        "daily-primary",
        "investment-specialist",
        "mathematical-reasoning-specialist",
        "coding-specialist",
    ]
    assert sum(item["primary"] is True for item in catalog) == 1
    assert {item["database_scope"] for item in catalog} == {
        "main",
        "investment",
        "mathematical",
        "coding",
    }
    assert catalog[0]["external_collaboration"] == "disabled"
    assert all(item["external_collaboration"] == "disabled" for item in catalog[1:])


def test_star_composes_modules_for_each_task_type() -> None:
    modules = StarModuleRegistry()
    investment = modules.plan(
        ["analysis", "risk"], coordinator_model="star-main-native-model"
    )
    mathematical = modules.plan(
        ["calculation"], coordinator_model="star-main-native-model"
    )

    investment_ids = {item["module_id"] for item in investment["modules"]}
    mathematical_ids = {item["module_id"] for item in mathematical["modules"]}
    assert "investment-analysis" in investment_ids
    assert "mathematical-reasoning" not in investment_ids
    assert "mathematical-reasoning" in mathematical_ids
    assert {
        "language-understanding",
        "context-retrieval",
        "response-generation",
        "quality-governance",
        "self-training",
    } <= investment_ids & mathematical_ids


def test_star_roles_use_four_isolated_runtime_instances() -> None:
    registry = StarModelRegistry()
    engines = StarModelEngines(registry)
    role_engines = [
        engines.for_profile(registry.MAIN),
        engines.for_profile(registry.INVESTMENT),
        engines.for_profile(registry.MATHEMATICAL),
        engines.for_profile(registry.CODING),
    ]

    assert len({id(engine) for engine in role_engines}) == 4
    assert len({id(engine.runtime) for engine in role_engines}) == 4
    assert engines.for_profile(registry.INVESTMENT).allowed_intents == (
        registry.investment_intents
    )
    assert engines.for_profile(registry.MATHEMATICAL).allowed_intents == (
        registry.mathematical_intents
    )
    assert engines.for_profile(registry.CODING).allowed_intents == (
        registry.coding_intents
    )


def test_native_language_core_generates_from_learned_token_probabilities() -> None:
    model = StarAutoregressiveLanguageModel(
        corpus={
            "capabilities": (
                "星澄會理解需求，並生成清楚且可核對的回答。",
                "星澄會保留證據，並說明仍然未知的資訊。",
            )
        }
    )

    generated = model.generate(
        intent="capabilities",
        prompt="請介紹能力",
        grounding="星澄會理解需求，並生成清楚且可核對的回答。",
    )

    assert generated["decoder"] == "autoregressive-probabilistic-decoder"
    assert generated["model_type"] == "weighted-backoff-token-ngram"
    assert generated["token_count"] > 0
    assert generated["grounding_fallback_used"] is False
    assert model.metrics()["weighted_transition_count"] > 0


def test_native_language_core_composes_chinese_characters_and_validates_all_facts() -> None:
    model = StarAutoregressiveLanguageModel()

    assert model.tokenize("星澄理解繁體中文") == list("星澄理解繁體中文")
    generated = model.generate(
        intent="reasoning",
        prompt="整理核定資訊",
        grounding="預算為NT$50,000，日期為2026年12月15日，聯絡team@example.com。",
    )

    assert generated["facts_preserved"] is True
    assert generated["facts_supported"] is True
    assert generated["missing_facts"] == {}
    assert generated["unsupported_facts"] == {}
    assert "NT$50,000" in generated["text"]
    assert "2026年12月15日" in generated["text"]
    assert "team@example.com" in generated["text"]


def test_native_language_core_conditions_generation_on_prompt_examples() -> None:
    model = StarAutoregressiveLanguageModel(
        corpus={
            "capabilities": (
                "遇到證據不足時要明確拒絕猜測。",
                "資料完整時要依照步驟整理答案。",
            )
        }
    )

    generated = model.generate(
        intent="capabilities",
        prompt="遇到證據不足時要明確拒絕猜測。",
        grounding="",
        temperature=0,
    )

    assert "證據不足" in generated["text"]
    assert generated["prompt_conditioned_example_count"] >= 1


def test_role_engines_have_distinct_role_corpora_and_metrics() -> None:
    registry = StarModelRegistry()
    engines = StarModelEngines(registry)
    statuses = {
        profile.role: engines.for_profile(profile).training_status()
        for profile in registry.profiles
    }

    assert {status["model_role"] for status in statuses.values()} == set(statuses)
    assert statuses[registry.MAIN.role]["base_example_count"] > statuses[
        registry.CODING.role
    ]["base_example_count"]
    assert len(
        {status["weighted_transition_count"] for status in statuses.values()}
    ) >= 3


def test_star_self_trains_verified_generation_and_restores_it(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)
    _, first = asyncio.run(
        service.handle("local_ai_infer", {"prompt": "請介紹你自己"})
    )

    assert first["mode"] == "native-generative-language-model"
    assert first["generation"]["decoder"] == "autoregressive-probabilistic-decoder"
    assert first["self_training"]["accepted"] is True
    assert first["self_training"]["learned_now"] is True
    module_ids = {
        item["module_id"] for item in first["module_execution"]["modules"]
    }
    assert {
        "language-understanding",
        "response-generation",
        "quality-governance",
        "self-training",
    } <= module_ids
    assert first["module_execution"]["mode"] == "automatic-composable-modules"
    main_repository = service.repositories[service.models.MAIN.model_id]
    assert main_repository.database_status()["tables"]["language_training_example"] == 1

    restarted = LocalAiService(tmp_path)
    restored = restarted.model_engines.main.training_status()
    assert restored["learned_example_count"] == 1
    _, duplicate = asyncio.run(
        restarted.handle("local_ai_infer", {"prompt": "請介紹你自己"})
    )
    assert duplicate["self_training"]["deduplicated"] is True


def test_star_self_training_isolated_by_specialist_database(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
            {
                "prompt": "請分析投資風險",
                "holdings": [
                    {"symbol": "TEST", "quantity": 1, "current_value_twd": 100}
                ],
            },
        )
    )

    assert result["self_training"]["model_id"] == "star-investment-native-model"
    assert service.repositories[
        service.models.INVESTMENT.model_id
    ].database_status()["tables"]["language_training_example"] == 1
    assert service.repositories[
        service.models.MAIN.model_id
    ].database_status()["tables"]["language_training_example"] == 0


def test_star_coding_expert_generates_ast_validated_python() -> None:
    result = StarCodingExpert().process(
        {
            "prompt": "建立加總函式",
            "code_spec": {
                "language": "python",
                "name": "calculate_total",
                "parameters": ["values"],
                "description": "加總輸入值",
                "return_expression": "sum(values)",
            },
        },
        "coding",
    )

    assert result["ok"] is True
    assert result["validation"]["ast_node_count"] > 0
    assert result["validation"]["executed"] is False
    assert "def calculate_total(values):" in result["source"]
    assert "return sum(values)" in result["source"]


def test_star_coding_expert_infers_common_code_from_natural_language() -> None:
    result = StarCodingExpert().process(
        {"prompt": "請寫一個計算平均值的 Python 函式"},
        "coding",
    )

    assert result["ok"] is True
    assert result["normalized_spec"]["name"] == "calculate_average"
    assert "def calculate_average(values):" in result["source"]
    assert "sum(values) / len(values)" in result["source"]


def test_star_coding_expert_builds_freeform_fastapi_transaction_module() -> None:
    result = StarCodingExpert().process(
        {
            "prompt": "請建立 FastAPI REST API，含驗證、資料庫交易、例外處理與單元測試"
        },
        "coding",
    )

    assert result["ok"] is True
    assert result["artifact_kind"] == "api"
    assert "app = FastAPI" in result["source"]
    assert "def database_transaction" in result["source"]
    assert "connection.rollback()" in result["source"]
    assert "HTTPException" in result["source"]
    assert result["generated_tests"]["available"] is True
    assert result["generated_tests"]["validation"]["ok"] is True
    compile(result["generated_tests"]["source"], "<star-api-tests>", "exec")
    compile(result["source"], "<star-natural-language-code>", "exec")


def test_star_coding_expert_supports_dataclasses_and_generated_tests() -> None:
    expert = StarCodingExpert()
    dataclass_result = expert.process(
        {
            "prompt": "建立持股資料類別",
            "code_spec": {
                "kind": "dataclass",
                "name": "Holding",
                "fields": [
                    {"name": "symbol", "type": "str", "default": ""},
                    {"name": "quantity", "type": "float", "default": 0},
                ],
            },
        },
        "coding",
    )
    function_result = expert.process(
        {
            "prompt": "建立加總函式與測試",
            "code_spec": {
                "kind": "function",
                "name": "calculate_total",
                "parameters": ["values"],
                "return_expression": "sum(values)",
                "test_cases": [
                    {"args": [[1, 2, 3]], "expected": 6},
                    {"args": [[]], "expected": 0},
                ],
            },
        },
        "coding",
    )

    assert dataclass_result["ok"] is True
    assert dataclass_result["artifact_kind"] == "dataclass"
    compile(dataclass_result["source"], "<star-dataclass>", "exec")
    assert function_result["generated_tests"]["available"] is True
    assert function_result["generated_tests"]["validation"]["ok"] is True
    compile(function_result["generated_tests"]["source"], "<star-tests>", "exec")


def test_star_coding_expert_analyzes_and_rejects_dangerous_code() -> None:
    expert = StarCodingExpert()
    dangerous = expert.process(
        {
            "prompt": "分析程式碼",
            "source_code": "import subprocess\nsubprocess.run(['tool'])\n",
            "code_spec": {"action": "analyze", "language": "python"},
        },
        "coding",
    )
    refactored = expert.process(
        {
            "prompt": "重構程式碼",
            "source_code": "def add(a,b):\n return a+b\n",
            "code_spec": {"action": "refactor", "language": "python"},
        },
        "coding",
    )

    assert dangerous["ok"] is False
    assert dangerous["validation"]["syntax_ok"] is True
    assert dangerous["validation"]["security_ok"] is False
    assert dangerous["validation"]["analysis"]["security_findings"]
    assert refactored["ok"] is True
    assert "def add(a, b):" in refactored["source"]


def test_star_coding_expert_rejects_dynamic_and_filesystem_bypasses() -> None:
    expert = StarCodingExpert()
    dangerous_sources = [
        "import builtins\nbuiltins.exec('value = 1')\n",
        "getattr(__builtins__, 'exec')('value = 1')\n",
        "import os\nos.remove('important.txt')\n",
        "from pathlib import Path\nPath('important.txt').unlink()\n",
        "with open('important.txt', 'w') as stream:\n    stream.write('x')\n",
    ]

    for source in dangerous_sources:
        result = expert.process(
            {
                "source_code": source,
                "code_spec": {"action": "analyze", "language": "python"},
            },
            "coding",
        )
        assert result["ok"] is False
        assert result["validation"]["security_ok"] is False
        assert result["validation"]["analysis"]["security_findings"]


def test_star_coding_expert_generates_typescript_and_javascript() -> None:
    expert = StarCodingExpert()
    typescript = expert.process(
        {"prompt": "請用 TypeScript 寫一個計算平均值的函式"},
        "coding",
    )
    javascript = expert.process(
        {
            "prompt": "建立 JavaScript 測試函式",
            "code_spec": {
                "language": "javascript",
                "kind": "test",
                "subject": "calculateTotal",
            },
        },
        "coding",
    )

    assert typescript["ok"] is True
    assert typescript["language"] == "typescript"
    assert "values: number[]" in typescript["source"]
    assert "): number {" in typescript["source"]
    assert "values.reduce" in typescript["source"]
    assert javascript["ok"] is True
    assert "function test_calculateTotal() {" in javascript["source"]
    assert ": void" not in javascript["source"]


def test_star_coding_expert_types_nullable_maximum_and_minimum() -> None:
    expert = StarCodingExpert()

    for operation in ("maximum", "minimum"):
        result = expert.process(
            {
                "code_spec": {
                    "language": "typescript",
                    "kind": "function",
                    "name": f"find_{operation}",
                    "parameters": ["values"],
                    "operation": operation,
                }
            },
            "coding",
        )
        assert result["ok"] is True
        assert "): number | null {" in result["source"]
        assert ": null" in result["source"]


def test_star_coding_expert_rejects_dangerous_javascript() -> None:
    result = StarCodingExpert().process(
        {
            "prompt": "分析 JavaScript 程式碼",
            "source_code": "export function run(source) { return eval(source); }\n",
            "code_spec": {"action": "analyze", "language": "javascript"},
        },
        "coding",
    )

    assert result["ok"] is False
    assert result["validation"]["syntax_ok"] is True
    assert result["validation"]["security_ok"] is False
    assert result["validation"]["analysis"]["security_findings"][0]["code"] == (
        "DANGEROUS_SCRIPT_PATTERN"
    )


def test_star_coding_expert_generates_parameterized_read_only_sql() -> None:
    expert = StarCodingExpert()
    generated = expert.process(
        {
            "prompt": "建立 SQL 查詢",
            "code_spec": {
                "language": "sql",
                "table": "holdings",
                "fields": ["symbol", "quantity"],
                "filters": {"account_id": "ignored-at-generation"},
                "order_by": "symbol",
                "limit": 50,
            },
        },
        "coding",
    )
    dangerous = expert.process(
        {
            "prompt": "分析 SQL",
            "source_code": "DELETE FROM holdings;",
            "code_spec": {"action": "analyze", "language": "sql"},
        },
        "coding",
    )
    string_literal = expert.process(
        {
            "prompt": "分析 SQL",
            "source_code": "SELECT 'DELETE' AS label FROM holdings;",
            "code_spec": {"action": "analyze", "language": "sql"},
        },
        "coding",
    )

    assert generated["ok"] is True
    assert generated["artifact_kind"] == "query"
    assert 'FROM "holdings"' in generated["source"]
    assert '"account_id" = :account_id' in generated["source"]
    assert generated["validation"]["analysis"]["read_only"] is True
    assert dangerous["ok"] is False
    assert dangerous["validation"]["security_ok"] is False
    assert string_literal["ok"] is True


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
                "target_path": "src/backend/services/local_ai/application/total.ts",
            },
        },
        "self_upgrade",
    )

    proposal = result["upgrade_proposal"]
    assert result["ok"] is True
    assert proposal["proposal_ready"] is True
    assert proposal["change_type"] == "modify"
    assert proposal["target"]["new_file_only"] is False
    assert "--- a/src/backend/services/local_ai/application/total.ts" in proposal[
        "unified_diff"
    ]
    assert "+++ b/src/backend/services/local_ai/application/total.ts" in proposal[
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
    service = LocalAiService(tmp_path)
    _, inference = asyncio.run(
        service.handle(
            "local_ai_infer",
            {"prompt": "請用 TypeScript 寫一個計算平均值的函式"},
        )
    )
    _, status = asyncio.run(service.handle("local_ai_status", {}))

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
    service = LocalAiService(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
            {
                "prompt": "請自我升級並建立新的程式模組",
                "code_spec": {
                    "language": "python",
                    "name": "new_capability",
                    "parameters": ["payload"],
                    "return_expression": "{'ok': True}",
                    "target_path": "src/backend/services/local_ai/application/generated_extension.py",
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
    assert result["instruction_execution"]["status"] == "completed"
    assert "program-synthesis" in {
        item["module_id"] for item in result["module_execution"]["modules"]
    }


def test_star_runs_autonomous_bounded_self_maintenance(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
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

    assert registry.for_command("local_ai_infer").role == "daily-primary"
    assert registry.for_command("local_ai_infer", intent="risk").role == "investment-specialist"
    assert registry.for_command("local_ai_infer", intent="calculation").role == "mathematical-reasoning-specialist"
    assert registry.for_command("local_ai_infer", intent="reasoning").role == "mathematical-reasoning-specialist"
    assert registry.for_command("local_ai_infer", intent="coding").role == "coding-specialist"
    assert registry.for_command("local_ai_infer", intent="self_upgrade").role == "coding-specialist"
    assert registry.for_command("local_ai_analyze_investments").role == "investment-specialist"
    assert registry.for_command("local_ai_search_investments").role == "daily-primary"
    assert registry.for_command("local_ai_manage_investment_accounting").role == "daily-primary"
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
    service = LocalAiService(tmp_path)

    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
            {"prompt": "請分析投資風險，再計算並整理資料"},
        )
    )

    arrangement = result["task_arrangement"]
    assert arrangement["mode"] == "automatic-seven-stage-primary-backup-workflow"
    assert arrangement["task_allocation_model"] == (
        "qwen3:30b-a3b-instruct-2507-q4_K_M"
    )
    assert arrangement["integration_model"] == "gpt-oss:20b"
    assert arrangement["manual_assignment_allowed"] is False
    assert arrangement["star_native_model_included"] is False
    assert arrangement["external_ai_used"] is False
    assert {
        task["assigned_model"] for task in arrangement["tasks"]
    } == {
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "qwen3.5:9b-q4_K_M",
    }
    assert all(task["star_native_model_included"] is False for task in arrangement["tasks"])


def test_external_ai_request_remains_disabled(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)

    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
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
        tmp_path / "runtime" / "state" / "models" / "main.sqlite3",
        tmp_path / "runtime" / "state" / "models" / "investment.sqlite3",
        tmp_path / "runtime" / "state" / "models" / "mathematical.sqlite3",
        tmp_path / "runtime" / "state" / "models" / "coding.sqlite3",
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
    service = LocalAiService(tmp_path)

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
                "local_ai_infer", {**payload, "allow_network": True}
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
    service = LocalAiService(tmp_path)

    _, fallback = asyncio.run(
        service.handle("local_ai_infer", {"prompt": "請計算這個結果"})
    )
    _, denied = asyncio.run(
        service.handle(
            "local_ai_infer",
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
    service = LocalAiService(tmp_path)

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
            _, result = await service.handle("local_ai_infer", payload)
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


def test_main_model_understands_and_executes_a_search_instruction(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)
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
            "local_ai_infer",
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
    service = LocalAiService(tmp_path)
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
            "local_ai_infer",
            {"prompt": "參考本機模型建議調整投資參數"},
        )
    )

    assert result["ok"] is True
    assert result["advisor"] == "deepseek-r1:8b-0528-qwen3-q4_K_M"
    assert result["reviewer"] == "gpt-oss:20b"
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
    service = LocalAiService(tmp_path)
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
    service = LocalAiService(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
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
    service = LocalAiService(tmp_path)
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
            "local_ai_memory_review",
            {"memory_id": memory_id, "action": "approve", "reviewer": "owner"},
        )
    )
    assert approved["ok"] is True
    assert service.memory_broker.context_for_external("investment", "analysis")

    _, revoked = asyncio.run(
        service.handle(
            "local_ai_memory_review",
            {"memory_id": memory_id, "action": "revoke", "reviewer": "owner"},
        )
    )
    assert revoked["ok"] is True
    assert service.memory_broker.context_for_external("investment", "analysis") == []


def test_dynamic_upgrade_evaluation_checks_live_components(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    _, evaluation = asyncio.run(service.handle("local_ai_evaluate_upgrade", {}))
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
    service = LocalAiService(tmp_path)

    _, result = asyncio.run(
        service.handle("local_ai_infer", {"prompt": "檢討星澄並修正"})
    )

    assert result["intent"] == "self_upgrade"
    assert result["self_repair"]["executed"] is True
    assert result["self_repair"]["governance_rule_modified"] is False
    assert result["self_repair"]["investment_database_write_performed"] is False
    assert result["instruction_execution"]["executed"] is True


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
    service = LocalAiService(tmp_path)
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

    _, result = asyncio.run(
        service.handle(
            "local_ai_infer",
            {
                "prompt": "請說明專案預算限制",
                "runtime_model": service.NATIVE_MODEL_ID,
                "_runtime_model_selection_authorized": True,
                "allow_network": False,
            },
        )
    )

    retrieval = result["context_retrieval"]
    assert retrieval["memory_grounding_applied"] is True
    assert retrieval["used_memory_count"] == 1
    assert retrieval["memory_ids"] == [memory_id]
    assert "NT$500,000" in result["response"]
    assert any(item.get("memory_id") == memory_id for item in result["evidence"])
    assert result["self_training"]["accepted"] is False


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


def test_capability_composition_is_owned_only_by_star_main_database(
    tmp_path: Path,
) -> None:
    main = LocalAiRepository(tmp_path, database_scope="main")
    coding = LocalAiRepository(tmp_path, database_scope="coding")
    composition = {
        "composition_id": "composition-test-1",
        "status": "approved",
        "implementation_target": "local-ai/src/example.py",
        "model_assignments": {"implementation": "gpt-oss:20b"},
        "model_discussion": {"voters": [], "inspection_results": []},
    }

    stored = main.store_capability_composition(composition)

    assert stored["owner_model_id"] == "star-main-native-model"
    assert main.database_status()["tables"]["capability_composition"] == 1
    assert coding.database_status()["tables"]["capability_composition"] == 0
    with pytest.raises(PermissionError, match="MODEL_DATABASE_ISOLATION_DENIED"):
        coding.store_capability_composition(composition)
