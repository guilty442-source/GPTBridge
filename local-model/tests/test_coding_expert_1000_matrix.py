from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from local_ai.application.coding_expert import StarCodingExpert


def _valid_generation_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    python_expressions = (
        "sum(values)",
        "len(values)",
        "max(values) if values else None",
        "min(values) if values else None",
        "sorted(values)",
    )
    script_operations = ("average", "sum", "maximum", "minimum", "count", "sort")

    for index in range(200):
        cases.append(
            {
                "id": f"python-generate-{index:03d}",
                "category": "python",
                "payload": {
                    "prompt": f"建立 Python 驗證函式 {index}",
                    "code_spec": {
                        "language": "python",
                        "kind": "function",
                        "name": f"python_case_{index}",
                        "parameters": ["values"],
                        "return_expression": python_expressions[
                            index % len(python_expressions)
                        ],
                    },
                },
                "intent": "coding",
            }
        )

    for language in ("typescript", "javascript"):
        for index in range(200):
            cases.append(
                {
                    "id": f"{language}-generate-{index:03d}",
                    "category": language,
                    "payload": {
                        "prompt": f"建立 {language} 驗證函式 {index}",
                        "code_spec": {
                            "language": language,
                            "kind": "function",
                            "name": f"script_case_{index}",
                            "parameters": ["values"],
                            "operation": script_operations[
                                index % len(script_operations)
                            ],
                        },
                    },
                    "intent": "coding",
                }
            )

    for index in range(200):
        cases.append(
            {
                "id": f"sql-read-only-{index:03d}",
                "category": "sql",
                "payload": {
                    "prompt": f"建立唯讀 SQL 查詢 {index}",
                    "code_spec": {
                        "language": "sql",
                        "table": f"holding_{index}",
                        "fields": ["symbol", f"metric_{index}"],
                        "filters": {f"account_{index}": index},
                        "order_by": "symbol",
                        "direction": "DESC" if index % 2 else "ASC",
                        "limit": index + 1,
                    },
                },
                "intent": "coding",
            }
        )
    return cases


def _rejection_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    dangerous_python = (
        "import subprocess\nsubprocess.run(['tool'])\n",
        "import ctypes\nvalue = ctypes.c_int(1)\n",
        "import winreg\nvalue = winreg.HKEY_CURRENT_USER\n",
        "value = eval('1 + 1')\n",
        "exec('value = 1')\n",
        "value = compile('1', '<case>', 'eval')\n",
        "module = __import__('os')\n",
        "import os\nos.system('tool')\n",
        "import shutil\nshutil.rmtree('temporary')\n",
        "from subprocess import run\nrun(['tool'])\n",
    )
    dangerous_script = (
        "export function run(source) { return eval(source); }\n",
        "export function run(source) { return new Function(source)(); }\n",
        "import child from 'child_process';\nexport function run() { return child; }\n",
        "export function run() { return process.binding('fs'); }\n",
    )
    dangerous_sql = (
        "INSERT INTO holdings(symbol) VALUES ('A');",
        "UPDATE holdings SET quantity = 0;",
        "DELETE FROM holdings;",
        "DROP TABLE holdings;",
        "ALTER TABLE holdings ADD COLUMN note TEXT;",
        "CREATE TABLE copied(value INTEGER);",
        "REPLACE INTO holdings(symbol) VALUES ('A');",
        "ATTACH DATABASE 'other.db' AS other;",
        "DETACH DATABASE other;",
        "PRAGMA table_info(holdings);",
    )
    invalid_targets = (
        "../outside_{index}.py",
        "src/backend/services/other/outside_{index}.py",
        "/absolute/outside_{index}.py",
        "src/backend/services/local_ai/application/wrong_{index}.js",
        "src/backend/services/local_ai/../outside_{index}.py",
    )

    for index in range(40):
        cases.append(
            {
                "id": f"reject-python-security-{index:03d}",
                "category": "reject-security",
                "payload": {
                    "prompt": "分析 Python 安全性",
                    "source_code": dangerous_python[index % len(dangerous_python)],
                    "code_spec": {"action": "analyze", "language": "python"},
                },
                "intent": "coding",
            }
        )

    for index in range(40):
        language = "typescript" if index % 2 else "javascript"
        cases.append(
            {
                "id": f"reject-{language}-security-{index:03d}",
                "category": "reject-security",
                "payload": {
                    "prompt": f"分析 {language} 安全性",
                    "source_code": dangerous_script[index % len(dangerous_script)],
                    "code_spec": {"action": "analyze", "language": language},
                },
                "intent": "coding",
            }
        )

    for index in range(40):
        cases.append(
            {
                "id": f"reject-sql-write-{index:03d}",
                "category": "reject-security",
                "payload": {
                    "prompt": "分析 SQL 唯讀限制",
                    "source_code": dangerous_sql[index % len(dangerous_sql)],
                    "code_spec": {"action": "analyze", "language": "sql"},
                },
                "intent": "coding",
            }
        )

    for index in range(40):
        cases.append(
            {
                "id": f"reject-upgrade-scope-{index:03d}",
                "category": "reject-scope",
                "payload": {
                    "prompt": "建立受控升級提案",
                    "code_spec": {
                        "language": "python",
                        "name": f"upgrade_case_{index}",
                        "return_expression": "True",
                        "target_path": invalid_targets[
                            index % len(invalid_targets)
                        ].format(index=index),
                    },
                },
                "intent": "self_upgrade",
            }
        )

    for index in range(40):
        cases.append(
            {
                "id": f"reject-json-syntax-{index:03d}",
                "category": "reject-syntax",
                "payload": {
                    "prompt": "分析 JSON 語法",
                    "source_code": f"{{unquoted_{index}: true}}",
                    "code_spec": {"action": "analyze", "language": "json"},
                },
                "intent": "coding",
            }
        )
    return cases


CODING_CAPABILITY_CASES = _valid_generation_cases() + _rejection_cases()
assert len(CODING_CAPABILITY_CASES) == 1000


@pytest.mark.parametrize(
    "case",
    CODING_CAPABILITY_CASES,
    ids=[case["id"] for case in CODING_CAPABILITY_CASES],
)
def test_star_coding_capability_1000_case_matrix(case: dict[str, Any]) -> None:
    result = StarCodingExpert().process(case["payload"], case["intent"])
    category = case["category"]

    if category in {"python", "typescript", "javascript", "sql"}:
        assert result["ok"] is True
        assert result["language"] == category
        assert result["validation"]["syntax_ok"] is True
        assert result["validation"]["security_ok"] is True
        if category == "python":
            compile(result["source"], f"<{case['id']}>", "exec")
        elif category == "sql":
            assert result["validation"]["analysis"]["read_only"] is True
            assert result["source"].lstrip().startswith("SELECT ")
            assert result["source"].count(";") == 1
        else:
            assert "export function script_case_" in result["source"]
            assert not result["validation"]["errors"]
        return

    if category == "reject-security":
        assert result["ok"] is False
        assert result["validation"]["security_ok"] is False
        assert result["validation"]["analysis"]["security_findings"]
    elif category == "reject-scope":
        assert result["ok"] is True
        assert result["upgrade_proposal"]["proposal_ready"] is False
        assert result["upgrade_proposal"]["target"]["within_local_ai_source"] is False
        assert result["upgrade_proposal"]["source_write_performed"] is False
    else:
        assert category == "reject-syntax"
        assert result["ok"] is False
        assert result["validation"]["syntax_ok"] is False
        assert result["validation"]["errors"]
