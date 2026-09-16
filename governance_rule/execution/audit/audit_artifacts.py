"""Codex, git tier, metadata contract, and embedded browser artifact checks."""

from __future__ import annotations

import json
from pathlib import Path

import governance_rule.execution.git_tiers
from governance_rule.execution.chinese_codex_mirror import load_chinese_codex_parts
from governance_rule.execution.codex_repository import (
    format_codex_version,
    load_governance_codex,
)


def check_codex_consistency(root: Path, errors: list[str]) -> None:
    """Verify the Chinese codex reference is synchronized with the authoritative codex."""
    # A279 certified tooling: governed repository load, read-only.
    codex = load_governance_codex()
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


def check_git_tiers(root: Path, errors: list[str]) -> None:
    """Verify git tier enforcement layers and hooks exist and are correct."""
    git_tiers_source = root / "governance_rule" / "execution" / "git_tiers" / "__init__.py"
    if not git_tiers_source.is_file():
        errors.append("git tier enforcement module is missing")
    else:
        git_tiers_text = git_tiers_source.read_text(encoding="utf-8")
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
        git_gate_text = git_gate_source.read_text(encoding="utf-8")
        if "from governance_rule.execution.git_tiers import" not in git_gate_text:
            errors.append("git gate wrapper does not import git_tiers module")

    hook_root = root / "governance_rule" / "git-hooks"
    for hook_name in ("pre-commit", "pre-merge-commit", "pre-push"):
        hook_source = hook_root / hook_name
        if not hook_source.is_file():
            errors.append(f"governed Git hook is missing: {hook_name}")
    pre_push_source = hook_root / "pre-push"
    if pre_push_source.is_file():
        hook_text = pre_push_source.read_text(encoding="utf-8")
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
        contract_text = metadata_contract.read_text(encoding="utf-8")
        for required in ("FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
                         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
                         "FIELD_STATUS", "ResourceMetadata", "validate_qdrant_payload"):
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
        reconcile_text = reconcile_module.read_text(encoding="utf-8")
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
    elif "class ReconcileService" not in reconciliation_owner.read_text(encoding="utf-8"):
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
        "025_sqlite_generation_fence.sql",
        "026_workload_class.sql",
        "027_two_stage_deletion.sql",
    ):
        if not (migrations_dir / migration_name).is_file():
            errors.append(f"SQL migration is missing: {migration_name}")


def check_sqlite_template(root: Path, errors: list[str]) -> None:
    """Verify the SQLite module template contains required tables."""
    sqlite_template = root / "shared-layer" / "sql" / "sqlite_module_template.sql"
    if not sqlite_template.is_file():
        errors.append("SQLite module template is missing")
    else:
        template_text = sqlite_template.read_text(encoding="utf-8")
        for required_table in ("schema_version", "module_metadata",
                               "resource_metadata", "audit_event", "reconcile_state"):
            if required_table not in template_text:
                errors.append(f"SQLite module template is missing table: {required_table}")
        # Authority marker (migration 019) + write provenance (migration 020)
        for required_column in ("authority_class", "executor_id", "correlation_id"):
            if required_column not in template_text:
                errors.append(
                    f"SQLite module template is missing column: {required_column}"
                )


def check_data_lineage(root: Path, errors: list[str]) -> None:
    """Verify data lineage migration (018) defines required objects."""
    lineage_migration = root / "shared-layer" / "migrations" / "018_data_lineage.sql"
    if not lineage_migration.is_file():
        errors.append("Data lineage migration 018 is missing")
        return
    text = lineage_migration.read_text(encoding="utf-8")
    for required_object in (
        "gptbridge_index.data_lineage",
        "auto_populate_lineage",
        "resource_lineage_populate",
        "resource_lineage",
    ):
        if required_object not in text:
            errors.append(
                f"Data lineage migration 018 is missing object: {required_object}"
            )


def check_authority_marker(root: Path, errors: list[str]) -> None:
    """Verify authority marker migration (019) defines required columns."""
    authority_migration = root / "shared-layer" / "migrations" / "019_authority_marker.sql"
    if not authority_migration.is_file():
        errors.append("Authority marker migration 019 is missing")
        return
    text = authority_migration.read_text(encoding="utf-8")
    for required_token in (
        "authority_class",
        "central-official",
        "module-private",
        "degraded-copy",
    ):
        if required_token not in text:
            errors.append(
                f"Authority marker migration 019 is missing token: {required_token}"
            )


def check_write_provenance(root: Path, errors: list[str]) -> None:
    """Verify write provenance migration (020) defines required objects."""
    provenance_migration = root / "shared-layer" / "migrations" / "020_write_provenance.sql"
    if not provenance_migration.is_file():
        errors.append("Write provenance migration 020 is missing")
        return
    text = provenance_migration.read_text(encoding="utf-8")
    for required_object in (
        "executor_id",
        "correlation_id",
        "source_revision",
        "auto_populate_provenance",
        "resource_provenance_populate",
        "audit_event_provenance_populate",
    ):
        if required_object not in text:
            errors.append(
                f"Write provenance migration 020 is missing object: {required_object}"
            )


def check_provenance_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime provenance helper module exists and exports."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "provenance.py"
    if not helper.is_file():
        errors.append("Provenance helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    for required_symbol in ("set_provenance", "clear_provenance", "gptbridge.actor_id"):
        if required_symbol not in text:
            errors.append(
                f"Provenance helper is missing symbol: {required_symbol}"
            )


def check_permission_snapshot(root: Path, errors: list[str]) -> None:
    """Verify permission snapshot migration (021) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "021_permission_snapshot.sql"
    if not migration.is_file():
        errors.append("Permission snapshot migration 021 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "permission_snapshot",
        "capture_permission_snapshot",
        "evaluated_roles",
        "evaluated_policies",
        "decision_summary",
    ):
        if required not in text:
            errors.append(f"Permission snapshot migration 021 is missing: {required}")


def check_schema_ownership_lock(root: Path, errors: list[str]) -> None:
    """Verify schema ownership lock migration (022) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "022_schema_ownership_lock.sql"
    if not migration.is_file():
        errors.append("Schema ownership lock migration 022 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "gptbridge_migration_owner",
        "ddl_guard",
        "ddl_guard_trigger",
        "is_migration_executor",
    ):
        if required not in text:
            errors.append(f"Schema ownership lock migration 022 is missing: {required}")


def check_ddl_audit(root: Path, errors: list[str]) -> None:
    """Verify DDL audit migration (023) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "023_ddl_audit.sql"
    if not migration.is_file():
        errors.append("DDL audit migration 023 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "ddl_event",
        "audit_ddl_event",
        "ddl_audit_trigger",
        "command_tag",
    ):
        if required not in text:
            errors.append(f"DDL audit migration 023 is missing: {required}")


def check_contract_handshake(root: Path, errors: list[str]) -> None:
    """Verify contract version handshake migration (024) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "024_contract_version_handshake.sql"
    if not migration.is_file():
        errors.append("Contract version handshake migration 024 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in (
        "contract_version",
        "check_contract_compatibility",
        "enforce_contract_version",
        "contract_version_fence",
        "min_compatible_version",
    ):
        if required not in text:
            errors.append(f"Contract handshake migration 024 is missing: {required}")


def check_permission_snapshot_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime permission snapshot helper exists."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "permission_snapshot.py"
    if not helper.is_file():
        errors.append("Permission snapshot helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    if "capture_snapshot" not in text:
        errors.append("Permission snapshot helper is missing: capture_snapshot")


def check_sqlite_generation_fence(root: Path, errors: list[str]) -> None:
    """Verify SQLite generation fence migration (025) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "025_sqlite_generation_fence.sql"
    if not migration.is_file():
        errors.append("SQLite generation fence migration 025 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("sqlite_generation", "upsert_sqlite_generation", "stale"):
        if required not in text:
            errors.append(f"SQLite generation fence migration 025 is missing: {required}")


def check_workload_class(root: Path, errors: list[str]) -> None:
    """Verify workload class migration (026) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "026_workload_class.sql"
    if not migration.is_file():
        errors.append("Workload class migration 026 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("workload_class", "interactive", "transport", "audit",
                     "reconciliation", "maintenance", "migration",
                     "statement_timeout_ms", "apply_workload_class"):
        if required not in text:
            errors.append(f"Workload class migration 026 is missing: {required}")


def check_two_stage_deletion(root: Path, errors: list[str]) -> None:
    """Verify two-stage deletion migration (027) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "027_two_stage_deletion.sql"
    if not migration.is_file():
        errors.append("Two-stage deletion migration 027 is missing")
        return
    text = migration.read_text(encoding="utf-8")
    for required in ("deletion_stage", "tombstone", "retention", "purged",
                     "tombstone_resource", "advance_deletion_stage",
                     "get_purge_eligible", "purge_after"):
        if required not in text:
            errors.append(f"Two-stage deletion migration 027 is missing: {required}")


def check_orphan_scanner(root: Path, errors: list[str]) -> None:
    """Verify the runtime orphan scanner module exists."""
    scanner = root / "shared-layer" / "src" / "shared_layer" / "database" / "orphan_scanner.py"
    if not scanner.is_file():
        errors.append("Orphan scanner module is missing")
        return
    text = scanner.read_text(encoding="utf-8")
    if "scan_orphans" not in text:
        errors.append("Orphan scanner is missing: scan_orphans")


def check_deletion_coordinator(root: Path, errors: list[str]) -> None:
    """Verify the runtime deletion coordinator module exists."""
    coordinator = root / "shared-layer" / "src" / "shared_layer" / "database" / "deletion_coordinator.py"
    if not coordinator.is_file():
        errors.append("Deletion coordinator module is missing")
        return
    text = coordinator.read_text(encoding="utf-8")
    for required in ("tombstone", "advance_stage", "get_purge_eligible"):
        if required not in text:
            errors.append(f"Deletion coordinator is missing: {required}")


def check_generation_fence_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime generation fence helper module exists."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "generation_fence.py"
    if not helper.is_file():
        errors.append("Generation fence helper module is missing")
        return
    text = helper.read_text(encoding="utf-8")
    for required in ("get_current_generation", "bump_generation", "is_connection_stale"):
        if required not in text:
            errors.append(f"Generation fence helper is missing: {required}")


def check_embedded_browser(root: Path, errors: list[str]) -> None:
    """Verify embedded browser enforcement: no Playwright, modules exist."""
    for module_path in (
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/browser_automation.py",
        "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/provider_session.py",
        "Standalone tools/vaultly/src/backend/services/vaultly/integration/browser_session.py",
    ):
        full_path = root / module_path
        if full_path.is_file():
            content = full_path.read_text(encoding="utf-8")
            if "from playwright" in content or "import playwright" in content:
                errors.append(f"module still uses Playwright: {module_path}")
            if "async_playwright" in content and "InProcessEmbeddedBrowser" not in content:
                errors.append(f"module still uses async_playwright: {module_path}")

    requirements = root / "main-system" / "requirements.txt"
    if requirements.is_file():
        req_text = requirements.read_text(encoding="utf-8")
        if "playwright" in req_text.lower():
            errors.append("main-system/requirements.txt still depends on playwright")

    pyproject = root / "main-system" / "pyproject.toml"
    if pyproject.is_file():
        py_text = pyproject.read_text(encoding="utf-8")
        if "playwright" in py_text.lower():
            errors.append("main-system/pyproject.toml still depends on playwright")

    embedded_browser = root / "main-system" / "src-ui" / "main" / "embedded-browser.ts"
    if not embedded_browser.is_file():
        errors.append("embedded browser module is missing")

    browser_client = root / "shared-layer" / "src" / "shared_layer" / "embedded_browser_client.py"
    if not browser_client.is_file():
        errors.append("embedded browser client module is missing")
