"""Orchestrator for the governance runtime audit."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)

from .audit_artifacts import (
    check_authority_marker,
    check_codex_consistency,
    check_contract_handshake,
    check_data_lineage,
    check_ddl_audit,
    check_deletion_coordinator,
    check_embedded_browser,
    check_generation_fence_helper,
    check_git_tiers,
    check_metadata_contract,
    check_orphan_scanner,
    check_permission_snapshot,
    check_permission_snapshot_helper,
    check_provenance_helper,
    check_readonly_domain_module,
    check_readonly_domain_startup_cert,
    check_rebuild_certification,
    check_rebuild_certifier_module,
    check_reconcile_modules,
    check_schema_ownership_lock,
    check_slo_metrics,
    check_sql_migrations,
    check_sqlite_template,
    check_sqlite_generation_fence,
    check_startup_certifier_module,
    check_two_stage_deletion,
    check_watchdog_bloat_rpo_rto,
    check_watchdog_module,
    check_workload_class,
    check_write_provenance,
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
    check_tool_isolation_hardening,
)
from .audit_activation import check_activation_states
from .audit_directories import check_directory_audit
from .audit_formal_rules import (
    check_formal_rules,
    check_implementation_obligations,
)
from .audit_manifests import (
    check_tool_identity_registration,
    check_tool_manifests,
)
from .audit_protected import (
    check_forbidden_legacy,
    check_protected_sources,
)
from .audit_runtime_contracts import check_runtime_contracts
from .audit_self_health import _verify_self_health_test_files

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _audit_workers(check_count: int) -> int:
    """Pick the audit thread count.

    The checks are a mix of I/O waits and pure-Python scanning; past four
    workers the GIL hand-off overhead outweighs the parallelism (measured
    on the 16-thread build machine: 8 workers were ~25% slower than 4).
    ``GPTBRIDGE_AUDIT_WORKERS`` overrides the default.
    """
    override = str(os.environ.get("GPTBRIDGE_AUDIT_WORKERS", "")).strip()
    if override.isdigit() and int(override) > 0:
        return max(1, min(int(override), check_count))
    return max(1, min(4, check_count))


def _sweep_stale_temp_files(directory: Path, max_age_seconds: float = 3600.0) -> None:
    """Remove abandoned atomic-write temp files left by crashed audits."""
    now = time.time()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(".tmp"):
            continue
        try:
            if now - entry.stat().st_mtime > max_age_seconds:
                os.unlink(entry.path)
        except OSError:
            continue


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
    audits retain the complete self-health barrier by default.  Every call
    performs the full read-only checks — no result is ever reused.
    """
    root = project_root.resolve()
    _sweep_stale_temp_files(Path(__file__).resolve().parent)

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
        lambda r: _collect(check_runtime_contracts, r),
        lambda r: _collect(check_identity_permissions, r),
        lambda r: _collect(check_main_system_source, r),
        lambda r: _collect(check_shared_layer_structure, r),
        lambda r: _collect(check_tool_isolation_hardening, r),
        _manifest_pair,
        lambda r: _collect(check_codex_consistency, r),
        lambda r: _collect(check_directory_audit, r),
        lambda r: _collect(check_activation_states, r),
        lambda r: _collect(check_formal_rules, r),
        lambda r: _collect(check_implementation_obligations, r),
        lambda r: _collect(check_git_tiers, r),
        lambda r: _collect(check_metadata_contract, r),
        lambda r: _collect(check_reconcile_modules, r),
        lambda r: _collect(check_sql_migrations, r),
        lambda r: _collect(check_sqlite_template, r),
        lambda r: _collect(check_data_lineage, r),
        lambda r: _collect(check_authority_marker, r),
        lambda r: _collect(check_write_provenance, r),
        lambda r: _collect(check_provenance_helper, r),
        lambda r: _collect(check_permission_snapshot, r),
        lambda r: _collect(check_schema_ownership_lock, r),
        lambda r: _collect(check_ddl_audit, r),
        lambda r: _collect(check_contract_handshake, r),
        lambda r: _collect(check_permission_snapshot_helper, r),
        lambda r: _collect(check_sqlite_generation_fence, r),
        lambda r: _collect(check_workload_class, r),
        lambda r: _collect(check_two_stage_deletion, r),
        lambda r: _collect(check_orphan_scanner, r),
        lambda r: _collect(check_deletion_coordinator, r),
        lambda r: _collect(check_generation_fence_helper, r),
        lambda r: _collect(check_rebuild_certification, r),
        lambda r: _collect(check_watchdog_bloat_rpo_rto, r),
        lambda r: _collect(check_readonly_domain_startup_cert, r),
        lambda r: _collect(check_slo_metrics, r),
        lambda r: _collect(check_rebuild_certifier_module, r),
        lambda r: _collect(check_watchdog_module, r),
        lambda r: _collect(check_startup_certifier_module, r),
        lambda r: _collect(check_readonly_domain_module, r),
        lambda r: _collect(check_embedded_browser, r),
    ]
    if include_self_health:
        checks.append(
            lambda r: _collect(_verify_self_health_test_files, r)
        )

    # Executor.map preserves submission order; a check raising is contained
    # as an error entry rather than aborting the remaining checks.
    with ThreadPoolExecutor(
        max_workers=_audit_workers(len(checks)),
        thread_name_prefix="governance-audit",
    ) as executor:
        results = list(executor.map(lambda check: check(root), checks))
    for result in results:
        errors.extend(result)

    return errors
