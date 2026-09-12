"""Orchestrator for the governance runtime audit."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

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
from .audit_activation import check_activation_states
from .audit_directories import check_directory_audit
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


def _collect(check: Callable[[Path, list[str]], None], root: Path) -> list[str]:
    errors: list[str] = []
    check(root, errors)
    return errors


def _manifest_pair(root: Path) -> list[str]:
    """Manifest checks are dependent — identity registration needs the
    manifest tool ids — so they run as one task."""
    errors: list[str] = []
    manifest_tool_ids, _ = check_tool_manifests(root, errors)
    check_tool_identity_registration(root, errors, manifest_tool_ids)
    return errors


def audit_runtime_governance(
    project_root: Path = PROJECT_ROOT,
    *,
    include_self_health: bool = True,
) -> list[str]:
    """Run governance checks and return errors.

    Checks are independent (each opens its own file handles and codex
    connections) so they run concurrently; the merged error list preserves
    the original declaration order, keeping results deterministic.
    Startup may omit subprocess-based test collection; release and explicit
    audits retain the complete self-health barrier by default.
    """
    root = project_root.resolve()
    errors: list[str] = []

    checks: list[Callable[[Path], list[str]]] = [
        source_ownership_errors,
        lambda r: _collect(check_authority_policy, r),
        lambda r: _collect(check_architecture_sources, r),
        lambda r: _collect(check_third_party_inventory, r),
        lambda r: _collect(check_shared_layer_policy, r),
        lambda r: _collect(check_repair_policy, r),
        lambda r: _collect(check_protected_sources, r),
        lambda r: _collect(check_forbidden_legacy, r),
        lambda r: _collect(check_identity_permissions, r),
        lambda r: _collect(check_main_system_source, r),
        lambda r: _collect(check_shared_layer_structure, r),
        _manifest_pair,
        lambda r: _collect(check_codex_consistency, r),
        lambda r: _collect(check_directory_audit, r),
        lambda r: _collect(check_activation_states, r),
        lambda r: _collect(check_git_tiers, r),
        lambda r: _collect(check_metadata_contract, r),
        lambda r: _collect(check_reconcile_modules, r),
        lambda r: _collect(check_sql_migrations, r),
        lambda r: _collect(check_sqlite_template, r),
        lambda r: _collect(check_embedded_browser, r),
    ]
    if include_self_health:
        checks.append(
            lambda r: _collect(_verify_self_health_test_files, r)
        )

    # Executor.map preserves submission order; a check raising is contained
    # as an error entry rather than aborting the remaining checks.
    with ThreadPoolExecutor(
        max_workers=min(8, len(checks)),
        thread_name_prefix="governance-audit",
    ) as executor:
        results = list(executor.map(lambda check: check(root), checks))
    for result in results:
        errors.extend(result)

    return errors
