"""governance_rule consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

import json
import sqlite3

from governance_rule.execution.codex_repository import (
    CODEX_DATABASE,
    GOVERNANCE_CODEX,
    format_codex_version,
)
from governance_rule.execution.audit.audit_directories import (
    check_directory_audit,
    check_directory_catalog_coverage,
    check_provision_classification,
)
from governance_rule.execution.git_tiers import classify
from governance_rule.governance_policy import GOVERNANCE_POLICY


def test_governance_codex_is_one_layered_declarative_database() -> None:
    assert CODEX_DATABASE.is_file()
    with sqlite3.connect(f"file:{CODEX_DATABASE.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "metadata", "sections", "principles", "articles", "edicts",
        "sovereigns", "savings", "source_exceptions",
    }.issubset(tables)
    assert GOVERNANCE_CODEX.savings.function == "none"
    assert GOVERNANCE_CODEX.savings.overriding_authority == "none-codex-has-no-power"


def test_governance_manifest_declares_collectable_self_health_target() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "governance_rule"
    assert manifest["test_targets"] == ["tests/test_governance_health.py"]
    assert (tool_root / manifest["test_targets"][0]).is_file()


def test_codex_is_the_enforcement_policy_source() -> None:
    assert GOVERNANCE_CODEX.codex_version > 0
    assert GOVERNANCE_CODEX.schema.endswith(
        f"v{format_codex_version(GOVERNANCE_CODEX.codex_version)}"
    )
    assert GOVERNANCE_POLICY.top_level_rule == "governance_codex"
    assert GOVERNANCE_POLICY.governance_rule_sources == (
        "governance_rule/codex/__init__.py",
    )


def test_data_roles_and_unknown_git_operations_fail_closed() -> None:
    responsibilities = GOVERNANCE_POLICY.system_responsibilities
    assert responsibilities.sql == "structured-mutable-official-data-postgresql"
    assert "owner-private" in responsibilities.sqlite
    assert responsibilities.qdrant_rag == "qdrant-semantic-knowledge-index"
    assert "never-canonical" in responsibilities.local_vector_fallback
    assert classify("unknown-governance-operation") == 3


def test_codex_authoritative_directories_are_readable_and_seeded() -> None:
    directories = GOVERNANCE_CODEX.directories
    assert {
        "fault_code_directory",
        "command_code_directory",
        "maintenance_manual_directory",
        "test_flow_directory",
        "project_architecture_directory",
    }.issubset(directories)
    fault = next(
        row for row in directories["fault_code_directory"]
        if row.get("fault_code") == "GOV_UNREGISTERED_FAULT_CODE"
    )
    assert fault.get("fault_code") == "GOV_UNREGISTERED_FAULT_CODE"
    assert fault.get("canonical_name") == "unregistered-fault-code"
    assert fault.get("severity") == "error"
    command = next(
        row for row in directories["command_code_directory"]
        if row.get("command_code") == "SYS_GPTBRIDGE_START"
    )
    assert command.get("command_code") == "SYS_GPTBRIDGE_START"
    assert command.get("canonical_command_id") == "system-start-gptbridge"
    for table, rows in directories.items():
        assert table.endswith("_directory")
        assert len({row.get(row.fields[0][0]) for row in rows}) == len(rows)


def test_directory_special_law_audit_gate_passes() -> None:
    errors: list[str] = []
    check_directory_audit(_ROOT, errors)
    assert errors == []


def test_master_catalog_covers_every_active_directory() -> None:
    errors: list[str] = []
    check_directory_catalog_coverage(_ROOT, errors)
    assert errors == []
    with sqlite3.connect(
        f"file:{CODEX_DATABASE.as_posix()}?mode=ro&immutable=1", uri=True
    ) as connection:
        master_codes = {
            row[0] for row in connection.execute("SELECT directory_code FROM directory_master_catalog")
        }
        contract_codes = {
            row[0] for row in connection.execute("SELECT directory_code FROM directory_format_contract")
        }
    assert contract_codes == master_codes
    assert len(master_codes) >= 20


def test_provision_classification_is_exactly_once() -> None:
    errors: list[str] = []
    check_provision_classification(_ROOT, errors)
    assert errors == []
    with sqlite3.connect(
        f"file:{CODEX_DATABASE.as_posix()}?mode=ro&immutable=1", uri=True
    ) as connection:
        classification_count = connection.execute(
            "SELECT COUNT(*) FROM provision_law_classification"
        ).fetchone()[0]
        provision_count = sum(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("principles", "articles", "edicts", "sovereigns")
        )
    assert classification_count == provision_count
