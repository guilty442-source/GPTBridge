from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "local-model" / "src" / "backend" / "services"
PACKAGE = SERVICES / "local_ai"
sys.path.insert(0, str(SERVICES))

from local_ai.domain.model_registry import StarModelRegistry
from local_ai.application.service import LocalAiService
from local_ai.infrastructure.repository import LocalAiRepository
from governance_rule.permission_directory.registries.permissions.tool_routes import (
    LOCAL_AI_AUTOMATIC_WORKFLOW_SEQUENCE,
)


def test_local_ai_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_local_ai_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (ROOT / "local-model" / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_automatic_workflow_matches_governance_and_manifest() -> None:
    manifest = json.loads(
        (ROOT / "local-model" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest_sequence = manifest["capabilities"]["local-ai"][
        "automatic_workflow"
    ]["sequence"]
    assert LocalAiService.AUTOMATIC_WORKFLOW_SEQUENCE == (
        LOCAL_AI_AUTOMATIC_WORKFLOW_SEQUENCE
    )
    assert manifest_sequence == list(LOCAL_AI_AUTOMATIC_WORKFLOW_SEQUENCE)


def test_star_models_have_four_isolated_databases(tmp_path: Path) -> None:
    registry = StarModelRegistry()
    profiles = registry.profiles
    repositories = [
        LocalAiRepository(tmp_path, database_scope=profile.database_scope)
        for profile in profiles
    ]
    paths = [repository.database_path.resolve() for repository in repositories]
    assert len(set(paths)) == 4
    assert {path.name for path in paths} == {
        "main.sqlite3",
        "investment.sqlite3",
        "mathematical.sqlite3",
        "coding.sqlite3",
    }
    assert all(path.is_relative_to(tmp_path.resolve()) for path in paths)
    assert all(path.is_file() for path in paths)


def test_specialist_network_and_database_policies_are_fixed() -> None:
    registry = StarModelRegistry()
    assert registry.MAIN.network_policy == "public-web-read-only"
    assert registry.INVESTMENT.network_policy == "disabled"
    assert registry.MATHEMATICAL.network_policy == "disabled"
    assert registry.CODING.network_policy == "disabled"
    assert len(
        {
            registry.MAIN.database_scope,
            registry.INVESTMENT.database_scope,
            registry.MATHEMATICAL.database_scope,
            registry.CODING.database_scope,
        }
    ) == 4
    with pytest.raises(PermissionError):
        registry.authorize_delegation(registry.INVESTMENT.model_id, registry.MAIN)


def test_xingcheng_role_setting_is_optional_single_personality_record() -> None:
    identity_directory = ROOT / "local-model" / "local-ai" / "databases" / "identity"
    identity_modules = sorted(identity_directory.glob("*.sql"))
    identity_sql = "\n".join(
        module.read_text(encoding="utf-8") for module in identity_modules
    )
    cognition_sql = (
        ROOT / "local-model" / "local-ai" / "databases" / "cognition.sql"
    ).read_text(encoding="utf-8")
    contract = json.loads(
        (ROOT / "main-system" / "config" / "data-architecture-contract.json")
        .read_text(encoding="utf-8")
    )

    role_setting = contract["xingcheng"]["role_setting"]
    assert role_setting == {
        "type": "personality",
        "database_isolation": "dedicated",
        "layers": ["role_data", "role_history", "role_audit"],
        "identity_format": (
            "{platform_id}:{module_id}:{data_category}:"
            "{resource_type}:{resource_id}"
        ),
        "maximum_records": 1,
        "initial_record_count": 0,
        "population_owner": "model-dialogue",
        "bootstrap_seed": False,
        "empty_record_count_is_valid": True,
    }
    assert [module.name for module in identity_modules] == [
        "001_role_data.sql",
        "002_role_history.sql",
        "003_role_audit.sql",
        "004_access_policy.sql",
    ]
    assert "CREATE SCHEMA IF NOT EXISTS role_data" in identity_sql
    assert "CREATE SCHEMA IF NOT EXISTS role_history" in identity_sql
    assert "CREATE SCHEMA IF NOT EXISTS role_audit" in identity_sql
    assert "role_data_single_personality" in identity_sql
    assert "INSERT INTO role_data.personality" not in identity_sql
    assert "cognition.model_data" in cognition_sql
    assert "cognition.model_setting" not in cognition_sql
    assert contract["xingcheng"]["legacy_model_settings_integration"] == "model-data"

    provisioner = (
        ROOT / "main-system" / "scripts" / "provision_postgresql_architecture.py"
    ).read_text(encoding="utf-8")
    assert "_integrate_legacy_xingcheng_model_data" in provisioner
    assert "DROP TABLE IF EXISTS identity.role_profile" in provisioner
    assert "_execute_sql_modules" in provisioner
    assert "legacy-role-profile" in provisioner
