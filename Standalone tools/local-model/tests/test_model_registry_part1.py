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

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]

from xingcheng.domain.model_registry import StarModelRegistry
from xingcheng.domain.module_registry import StarModuleRegistry
from xingcheng.integration.memory_broker import StarMemoryBroker
from xingcheng.infrastructure.repository import LocalAiRepository
from xingcheng.infrastructure.ollama_model_repository import OllamaModelRepository
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.model_engines import StarModelEngines
from xingcheng.infrastructure.native_model import StarNativeLanguageModel
from xingcheng.infrastructure import repository as repository_module
from xingcheng.application.service import LocalAiService
from xingcheng.application.investment_accounting import coordinate_investment_accounting
from xingcheng.application.investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from xingcheng.application.coding_expert import StarCodingExpert




class _FakeOllamaTransport:
    """Minimal fake Ollama transport for tests that need transformer enabled."""

    def __init__(self, response_text: str = "我是星澄的本機 Transformer 語言模型。") -> None:
        self.response_text = response_text
        self.calls: list[tuple[str, str, dict[str, Any] | None, float]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((method, url, payload, timeout))
        if url.endswith("/api/tags"):
            return {"models": [{"name": StarTransformerRuntime.MODEL}]}
        if url.endswith("/api/version"):
            return {"version": "test"}
        if url.endswith("/api/chat"):
            return {
                "message": {"role": "assistant", "content": self.response_text},
                "prompt_eval_count": 120,
                "eval_count": 18,
                "load_duration": 10,
                "total_duration": 20,
            }
        if url.endswith("/api/embed"):
            inputs = payload.get("input") if isinstance(payload, dict) else []
            return {"embeddings": [[1.0, float(i + 1)] for i, _ in enumerate(inputs if isinstance(inputs, list) else [])]}
        raise AssertionError(f"unexpected URL: {url}")




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
    service = _make_service(tmp_path)
    _, first = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "請介紹你自己"})
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

    restarted = _make_service(tmp_path)
    restored = restarted.model_engines.main.training_status()
    assert restored["learned_example_count"] == 1
    _, duplicate = asyncio.run(
        restarted.handle("xingcheng_infer", {"prompt": "請介紹你自己"})
    )
    assert duplicate["self_training"]["deduplicated"] is True


def test_star_self_training_isolated_by_specialist_database(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
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
