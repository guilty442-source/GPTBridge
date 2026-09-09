"""Codex, git tier, metadata contract, and embedded browser artifact checks."""

from __future__ import annotations

import json
from pathlib import Path

import governance_rule.execution.git_tiers
from governance_rule.codex import GOVERNANCE_CODEX


def check_codex_consistency(root: Path, errors: list[str]) -> None:
    """Verify the Chinese codex reference is synchronized with the authoritative codex."""
    chinese_path = root / "governance_rule" / "codex" / "governance_codex.zh-TW.txt"
    try:
        chinese = json.loads(chinese_path.read_text(encoding="utf-8"))
        tables = chinese["tables"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        errors.append(f"Chinese codex reference is invalid: {error}")
        chinese, tables = {}, {}
    if str(chinese.get("codex_version")) != f"{GOVERNANCE_CODEX.codex_version:.5f}":
        errors.append("Chinese codex version is not synchronized")
    expected_ids = {
        "principles": {item.id for item in GOVERNANCE_CODEX.principles},
        "articles": {item.id for item in GOVERNANCE_CODEX.articles},
        "edicts": {item.id for item in GOVERNANCE_CODEX.edicts},
        "sovereigns": {item.id for item in GOVERNANCE_CODEX.sovereigns},
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


def check_embedded_browser(root: Path, errors: list[str]) -> None:
    """Verify embedded browser enforcement: no Playwright, modules exist."""
    for module_path in (
        "ai-collaboration/src/backend/services/ai_collaboration/integration/browser_automation.py",
        "ai-collaboration/src/backend/services/ai_collaboration/integration/provider_session.py",
        "vaultly/src/backend/services/vaultly/integration/browser_session.py",
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
