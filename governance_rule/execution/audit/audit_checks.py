"""Orchestrator for the governance runtime audit."""

from __future__ import annotations

from pathlib import Path

from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)

from .audit_artifacts import (
    check_codex_consistency,
    check_embedded_browser,
    check_git_tiers,
    check_metadata_contract,
    check_reconcile_modules,
    check_sql_migrations,
    check_sqlite_template,
)
from .audit_authority import (
    check_architecture_sources,
    check_identity_permissions,
    check_main_system_source,
    check_authority_policy,
    check_repair_policy,
    check_shared_layer_policy,
    check_shared_layer_structure,
    check_third_party_inventory,
)
from .audit_manifests import (
    check_tool_identity_registration,
    check_tool_manifests,
)
from .audit_protected import (
    check_forbidden_legacy,
    check_protected_sources,
)
from .audit_self_health import _verify_self_health_test_files

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def audit_runtime_governance(project_root: Path = PROJECT_ROOT) -> list[str]:
    """Run all governance runtime checks and return a list of errors."""
    root = project_root.resolve()
    errors: list[str] = []

    errors.extend(source_ownership_errors(root))

    check_authority_policy(root, errors)
    check_architecture_sources(root, errors)
    check_third_party_inventory(root, errors)
    check_shared_layer_policy(root, errors)
    check_repair_policy(root, errors)
    check_protected_sources(root, errors)
    check_forbidden_legacy(root, errors)
    check_identity_permissions(root, errors)
    check_main_system_source(root, errors)
    check_shared_layer_structure(root, errors)

    manifest_tool_ids, _ = check_tool_manifests(root, errors)
    check_tool_identity_registration(root, errors, manifest_tool_ids)

    check_codex_consistency(root, errors)
    check_git_tiers(root, errors)
    check_metadata_contract(root, errors)
    check_reconcile_modules(root, errors)
    check_sql_migrations(root, errors)
    check_sqlite_template(root, errors)
    check_embedded_browser(root, errors)

    _verify_self_health_test_files(root, errors)

    return errors
