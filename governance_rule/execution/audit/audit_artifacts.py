"""Codex, git tier, metadata contract, and embedded browser artifact checks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ._file_cache import read_text_cached

import governance_rule.execution.git_tiers
from governance_rule.execution.chinese_codex_mirror import load_chinese_codex_parts
from governance_rule.execution.codex_repository import (
    format_codex_version,
    load_governance_codex,
)

_TEXT_POLLUTION = re.compile(
    r"\?{2,}|\ufffd|\ufeff|ï»¿|Ã.|Â.|â(?:€|€™|€œ|€\x9d)|[\ue000-\uf8ff]"
)

def _contains_text_pollution(value: object) -> bool:
    """Return whether a nested Codex value contains lossy or invalid text."""
    if isinstance(value, str):
        return bool(_TEXT_POLLUTION.search(value)) or any(
            ord(character) < 32 and character not in "\n\r\t" for character in value
        )
    if isinstance(value, dict):
        return any(_contains_text_pollution(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text_pollution(item) for item in value)
    return False

def _contains_text_pollution(value: object) -> bool:
    """Return whether a nested Codex value contains lossy or invalid text."""
    if isinstance(value, str):
        return bool(_TEXT_POLLUTION.search(value)) or any(
            ord(character) < 32 and character not in "\n\r\t" for character in value
        )
    if isinstance(value, dict):
        return any(_contains_text_pollution(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text_pollution(item) for item in value)
    return False

def check_codex_consistency(root: Path, errors: list[str]) -> None:
    """Verify the Chinese codex reference is synchronized with the authoritative codex."""
    # A279 certified tooling: governed repository load, read-only.
    try:
        codex = load_governance_codex()
    except Exception as error:
        errors.append(f"governance codex is unreadable: {error}")
        return
    try:
        chinese = load_chinese_codex_parts(root / "governance_rule" / "codex")
        tables = chinese["tables"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        errors.append(f"Chinese codex reference is invalid: {error}")
        chinese, tables = {}, {}
    if str(chinese.get("codex_version")) != format_codex_version(
        codex.codex_version
    ):
        errors.append("Chinese codex version is not synchronized")
    expected_ids = {
        "principles": {item.id for item in codex.principles},
        "articles": {item.id for item in codex.articles},
        "edicts": {item.id for item in codex.edicts},
        "sovereigns": {item.id for item in codex.sovereigns},
    }
    for table_name, expected in expected_ids.items():
        key = "sovereign_id" if table_name == "sovereigns" else "provision_id"
        actual = {str(row.get(key)) for row in tables.get(table_name, [])}
        if actual != expected:
            errors.append(f"Chinese codex identities are not synchronized: {table_name}")
    for required_table in (
        "metadata", "revision_history", "seal_manifest", "certification_policy",
        "version_evolution_rules", "provision_identities", "provision_lineage",
    ):
        if required_table not in tables:
            errors.append(f"Chinese codex metadata is missing: {required_table}")
    if _contains_text_pollution(tables):
        errors.append("Codex or Chinese codex contains text pollution")
    architecture_root = root / "governance_rule" / "codex"
    for path in architecture_root.glob("architecture-*.md"):
        try:
            content = read_text_cached(path)
        except (OSError, UnicodeError) as error:
            errors.append(f"architecture text is invalid: {path.name}: {error}")
            continue
        if _contains_text_pollution(content):
            errors.append(f"architecture text contains pollution: {path.name}")

def check_git_tiers(root: Path, errors: list[str]) -> None:
    """Verify git tier enforcement layers and hooks exist and are correct."""
    git_tiers_source = root / "governance_rule" / "execution" / "git_tiers" / "__init__.py"
    if not git_tiers_source.is_file():
        errors.append("git tier enforcement module is missing")
    else:
        git_tiers_text = read_text_cached(git_tiers_source)
        if "TIER1_OPS" not in git_tiers_text or "TIER2_OPS" not in git_tiers_text or "TIER3_OPS" not in git_tiers_text:
            errors.append("git tier module is missing tier operation sets")
        if "def classify" not in git_tiers_text or "def enforce" not in git_tiers_text:
            errors.append("git tier module is missing classify/enforce functions")
        if "def audit_log" not in git_tiers_text:
            errors.append("git tier module is missing audit_log function")
        if governance_rule.execution.git_tiers.classify("unknown-governance-operation") != 3:
            errors.append("unknown git operations must fail closed as tier 3")

    git_gate_source = root / "scripts" / "git-gate.py"
    if not git_gate_source.is_file():
        errors.append("git gate wrapper is missing")
    else:
        git_gate_text = read_text_cached(git_gate_source)
        if "from governance_rule.execution.git_tiers import" not in git_gate_text:
            errors.append("git gate wrapper does not import git_tiers module")

    hook_root = root / "governance_rule" / "git-hooks"
    for hook_name in ("pre-commit", "pre-merge-commit", "pre-push"):
        hook_source = hook_root / hook_name
        if not hook_source.is_file():
            errors.append(f"governed Git hook is missing: {hook_name}")
    pre_push_source = hook_root / "pre-push"
    if pre_push_source.is_file():
        hook_text = read_text_cached(pre_push_source)
        if "GOVERNANCE_AUTHORITY_APPROVAL" not in hook_text:
            errors.append("pre-push hook does not enforce governance authority approval")
        if "merge-base" not in hook_text or "refs/tags/" not in hook_text:
            errors.append("pre-push hook does not detect non-fast-forward or tag rewrites")

def check_metadata_contract(root: Path, errors: list[str]) -> None:
    """Verify metadata contract module and data ownership document exist."""
    metadata_contract = root / "shared-layer" / "src" / "shared_layer" / "metadata_contract.py"
    if not metadata_contract.is_file():
        errors.append("metadata contract module is missing")
    else:
        contract_text = read_text_cached(metadata_contract)
        for required in ("FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
                         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
                         "FIELD_STATUS", "ResourceMetadata", "validate_vector_payload"):
            if required not in contract_text:
                errors.append(f"metadata contract is missing: {required}")

    ownership_doc = root / "shared-layer" / "docs" / "DATA_OWNERSHIP_CONTRACT.md"
    if not ownership_doc.is_file():
        errors.append("data ownership contract document is missing")

def check_reconcile_modules(root: Path, errors: list[str]) -> None:
    """Verify reconcile state store and system reconciliation owner exist."""
    reconcile_module = root / "shared-layer" / "src" / "shared_layer" / "reconcile.py"
    if not reconcile_module.is_file():
        errors.append("reconcile state store module is missing")
    else:
        reconcile_text = read_text_cached(reconcile_module)
        if "ReconcileStateStore" not in reconcile_text:
            errors.append("reconcile module is missing ReconcileStateStore class")
        for forbidden_decision in (
            "class ReconcileService",
            "def _push_to_central",
            "def _pull_from_central",
        ):
            if forbidden_decision in reconcile_text:
                errors.append(
                    f"shared layer contains reconciliation decision: {forbidden_decision}"
                )
    reconciliation_owner = (
        root / "main-system" / "src-core" / "core_system" / "data_reconciliation.py"
    )
    if not reconciliation_owner.is_file():
        errors.append("system data reconciliation owner is missing")
    elif "class ReconcileService" not in read_text_cached(reconciliation_owner):
        errors.append("system data reconciliation owner lacks decision service")

def check_sql_migrations(root: Path, errors: list[str]) -> None:
    """Verify required SQL migration files exist."""
    migrations_dir = root / "shared-layer" / "migrations"
    for migration_name in (
        "004_global_and_module_version_tables.sql",
        "005_central_index_composite_indexes.sql",
        "006_audit_append_only_enforcement.sql",
        "007_transport_idempotency_key.sql",
        "008_rls_role_isolation.sql",
        "018_data_lineage.sql",
        "019_authority_marker.sql",
        "020_write_provenance.sql",
        "021_permission_snapshot.sql",
        "022_schema_ownership_lock.sql",
        "023_ddl_audit.sql",
        "024_contract_version_handshake.sql",
        "026_workload_class.sql",
        "027_two_stage_deletion.sql",
        "028_rebuild_certification.sql",
        "029_watchdog_bloat_rpo_rto.sql",
        "030_readonly_domain_startup_cert.sql",
        "031_slo_metrics.sql",
        "032_workload_pool_query_class.sql",
        "033_query_fingerprint.sql",
        "034_transport_hot_path_index.sql",
        "035_audit_hot_history_separation.sql",
        "036_wal_checkpoint_monitor.sql",
        "038_incremental_reconcile.sql",
        "039_performance_baseline.sql",
        "040_database_release_manifest.sql",
        "041_compatibility_matrix.sql",
        "042_migration_breaking_change.sql",
        "043_query_contract_version.sql",
        "044_rls_role_migration.sql",
        "047_canary_upgrade.sql",
        "048_release_audit.sql",
        "049_roll_forward.sql",
        "058_transport_priority_queue.sql",
        "050_unified_lifecycle_state.sql",
        "051_transport_retention.sql",
        "052_audit_retention_layering.sql",
        "055_purge_queue.sql",
        "056_archive_catalog.sql",
        "057_archive_versioning.sql",
        "059_retention_hold.sql",
        "060_dependency_check.sql",
        "061_archive_restore_test.sql",
        "062_capacity_quota.sql",
        "063_purge_audit.sql",
        "064_audit_hash_chain.sql",
        "065_reconcile_batch_digest.sql",
        "066_resource_content_hash.sql",
        "069_merkle_root.sql",
        "070_integrity_snapshot.sql",
        "071_restore_verification.sql",
        "072_tamper_state.sql",
        "073_fail_closed.sql",
        "074_version_lock.sql",
        "075_compatibility_matrix_ext.sql",
        "076_upgrade_classification.sql",
        "077_driver_compatibility_test.sql",
        "078_pg_major_upgrade_rehearsal.sql",
        "081_sbom_dependency_inventory.sql",
        "082_vulnerability_risk.sql",
        "083_dependency_drift.sql",
        "084_offline_bundle.sql",
        "085_release_signature.sql",
        "086_lineage_core.sql",
        "087_security_identity_control.sql",
        "088_recovery_plan.sql",
        "089_recovery_incident.sql",
        "090_recovery_state_machine.sql",
        "091_pg_offline_recovery.sql",
        "092_pg_recovery_verification.sql",
        "093_reconcile_recovery_phase.sql",
        "094_recovery_generation.sql",
        "095_recovery_barrier.sql",
        "096_transport_recovery.sql",
        "097_unknown_commit_resolution.sql",
        "098_lease_recovery.sql",
        "100_recovery_priority.sql",
        "105_backup_restore_orchestration.sql",
        "106_pitr_boundary.sql",
        "107_recovery_retry_policy.sql",
        "108_recovery_checkpoint.sql",
        "109_recovery_idempotency.sql",
        "110_recovery_safety_fence.sql",
        "111_chaos_drill.sql",
        "112_recovery_certification.sql",
        "113_workflow_operation.sql",
        "114_data_layer_contract.sql",
        "115_dependency_classification.sql",
        "116_startup_phase.sql",
        "117_startup_phase_gate.sql",
        "118_schema_readiness.sql",
        "119_rag_readiness_gate.sql",
        "120_shutdown_phase.sql",
        "121_shutdown_audit.sql",
        "122_unclean_shutdown_detection.sql",
        "123_cache_invalidation_policy.sql",
        "124_data_layer_dependency_graph.sql",
        "125_integration_rule.sql",
        "126_transport_lease_idempotency_catchup.sql",
    ):
        if not (migrations_dir / migration_name).is_file():
            errors.append(f"SQL migration is missing: {migration_name}")

_POOL_CALL = re.compile(r"(?:Thread|Process)PoolExecutor\s*\(")

_MAX_WORKERS_ARG = re.compile(r"max_workers\s*=\s*([^,\)]+)")

_CONST_INT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(\d+)\s*$", re.MULTILINE)

_IDENT_EXPR = re.compile(r"[A-Za-z_]\w*")

_POOL_BOUNDED_TOKENS = (
    "bounded_workers",
    "bounded_threads",
    "cpu_thread_budget",
    "apply_cpu_thread_budget",
    "_audit_workers",
    "min(",
)

_POOL_SCAN_ROOTS = (
    "main-system/src-core",
    "main-system/governance",
    "shared-layer/src",
    "governance_rule",
    "Standalone tools",
)

_POOL_SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "bin",
    "build",
    "dist",
    "node_modules",
    "releases",
    "runtime",
    "test",
    "tests",
}

_WORKER_POOL_CAP = 5

def _bounded_workers_expr(
    expr: str, text: str, consts: dict[str, int], depth: int = 0
) -> bool:
    """Return whether a ``max_workers`` expression is provably ≤ the cap."""
    expr = expr.strip()
    if not expr:
        return False
    if any(token in expr for token in _POOL_BOUNDED_TOKENS):
        return True
    if re.fullmatch(r"\d+", expr):
        return int(expr) <= _WORKER_POOL_CAP
    added = re.fullmatch(r"([A-Za-z_]\w*)\s*\+\s*(\d+)", expr)
    if added is not None and added.group(1) in consts:
        return consts[added.group(1)] + int(added.group(2)) <= _WORKER_POOL_CAP
    if _IDENT_EXPR.fullmatch(expr) and depth < 2:
        assign = re.search(
            rf"^\s*{re.escape(expr)}\s*=\s*(.+)$", text, re.MULTILINE
        )
        if assign is not None:
            return _bounded_workers_expr(
                assign.group(1), text, consts, depth + 1
            )
    return False

def check_bounded_worker_pools(root: Path, errors: list[str]) -> None:
    """Verify every worker pool is bounded inside the five-core budget.

    §10.30/A590: ``shared_layer.performance.thread_budget`` is the unified
    entry point (five-core cap, lower-only, ``threads × workers ≤ budget``);
    every ``ThreadPoolExecutor``/``ProcessPoolExecutor`` in production code
    must carry an explicit ``max_workers`` provably bounded — routed through
    the entry point, clamped via ``min()``, or a literal/constant ≤ 5.
    Unbounded ``max_workers=len(...)``-style pools fail closed.
    """
    module = (
        root
        / "shared-layer"
        / "src"
        / "shared_layer"
        / "performance"
        / "thread_budget.py"
    )
    if not module.is_file():
        errors.append("Thread budget module is missing")
        return
    module_text = read_text_cached(module)
    for required in (
        "CORE_BUDGET_CAP = 5",
        "bounded_workers",
        "bounded_threads",
        "allocation_within_budget",
    ):
        if required not in module_text:
            errors.append(f"Thread budget module is missing: {required}")
    for rel_root in _POOL_SCAN_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name for name in dirnames if name not in _POOL_SKIP_DIRS
            ]
            for filename in sorted(filenames):
                if not filename.endswith(".py") or filename.startswith("test_"):
                    continue
                path = Path(dirpath) / filename
                try:
                    if b"PoolExecutor" not in path.read_bytes():
                        continue
                except OSError:
                    continue
                text = read_text_cached(path)
                consts = {
                    name: int(value)
                    for name, value in _CONST_INT.findall(text)
                }
                for match in _POOL_CALL.finditer(text):
                    window = text[match.end() : match.end() + 240]
                    arg = _MAX_WORKERS_ARG.search(window)
                    rel_path = path.relative_to(root).as_posix()
                    if arg is None:
                        errors.append(
                            f"{rel_path}: worker pool missing explicit "
                            "max_workers"
                        )
                        continue
                    expr = arg.group(1).strip()
                    if not _bounded_workers_expr(expr, text, consts):
                        errors.append(
                            f"{rel_path}: worker pool not provably inside the "
                            f"five-core budget: max_workers={expr}"
                        )

def check_embedded_browser(root: Path, errors: list[str]) -> None:
    """Verify embedded browser enforcement: no Playwright, modules exist."""
    for module_path in (
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/browser_automation.py",
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/provider_session.py",
        "Standalone tools/vaultly/src/backend/services/vaultly/integration/browser_session.py",
    ):
        full_path = root / module_path
        if full_path.is_file():
            content = read_text_cached(full_path)
            if "from playwright" in content or "import playwright" in content:
                errors.append(f"module still uses Playwright: {module_path}")
            if "async_playwright" in content and "InProcessEmbeddedBrowser" not in content:
                errors.append(f"module still uses async_playwright: {module_path}")

    requirements = root / "main-system" / "requirements.txt"
    if requirements.is_file():
        req_text = read_text_cached(requirements)
        if "playwright" in req_text.lower():
            errors.append("main-system/requirements.txt still depends on playwright")

    pyproject = root / "main-system" / "pyproject.toml"
    if pyproject.is_file():
        py_text = read_text_cached(pyproject)
        if "playwright" in py_text.lower():
            errors.append("main-system/pyproject.toml still depends on playwright")

    embedded_browser = (
        root / "main-system" / "src-tauri" / "src" / "webview_host" / "mod.rs"
    )
    if not embedded_browser.is_file():
        errors.append("embedded browser module is missing")

    browser_client = root / "shared-layer" / "src" / "shared_layer" / "embedded_browser_client.py"
    if not browser_client.is_file():
        errors.append("embedded browser client module is missing")
