"""Release manifest and upgrade-flow audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_release_manifest_file(root: Path, errors: list[str]) -> None:
    """Verify the database-release.json manifest file exists."""
    manifest = root / "shared-layer" / "database-release.json"
    if not manifest.is_file():
        errors.append("database-release.json manifest is missing")
        return
    import json
    try:
        data = json.loads(read_text_cached(manifest))
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"database-release.json is invalid: {exc}")
        return
    for key in ("release_id", "schema_version", "migration_head",
                "rls_version", "role_version",
                "reconcile_contract_version",
                "query_contract_version", "minimum_runtime_version",
                "compatibility_range", "state"):
        if key not in data:
            errors.append(f"database-release.json is missing key: {key}")

def check_release_manifest_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime release manifest module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "release_manifest.py"
    if not module.is_file():
        errors.append("Release manifest module is missing")
        return
    text = read_text_cached(module)
    for required in ("load_manifest", "validate_runtime", "get_compatibility_matrix"):
        if required not in text:
            errors.append(f"Release manifest module is missing: {required}")

def check_query_contract_version(root: Path, errors: list[str]) -> None:
    """Verify query contract version migration (043) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "043_query_contract_version.sql"
    if not migration.is_file():
        errors.append("Query contract version migration 043 is missing")
        return
    text = read_text_cached(migration)
    for required in ("query_contract", "register_query_contract",
                     "deprecate_query_contract", "retire_query_contract",
                     "get_active_query_contract",
                     "active", "deprecated", "retired"):
        if required not in text:
            errors.append(f"Query contract version migration 043 is missing: {required}")

def check_rls_role_migration(root: Path, errors: list[str]) -> None:
    """Verify RLS/Role migration migration (044) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "044_rls_role_migration.sql"
    if not migration.is_file():
        errors.append("RLS/Role migration 044 is missing")
        return
    text = read_text_cached(migration)
    for required in ("rls_role_migration", "record_rls_role_migration",
                     "get_rls_role_version",
                     "create_role", "grant", "create_policy",
                     "security_definer_grant"):
        if required not in text:
            errors.append(f"RLS/Role migration 044 is missing: {required}")

def check_canary_upgrade(root: Path, errors: list[str]) -> None:
    """Verify canary upgrade migration (047) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "047_canary_upgrade.sql"
    if not migration.is_file():
        errors.append("Canary upgrade migration 047 is missing")
        return
    text = read_text_cached(migration)
    for required in ("canary_upgrade", "start_canary_upgrade",
                     "complete_canary_upgrade", "promote_canary_to_production",
                     "restoring", "migrating", "certifying",
                     "succeeded", "failed"):
        if required not in text:
            errors.append(f"Canary upgrade migration 047 is missing: {required}")

def check_release_audit(root: Path, errors: list[str]) -> None:
    """Verify release audit migration (048) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "048_release_audit.sql"
    if not migration.is_file():
        errors.append("Release audit migration 048 is missing")
        return
    text = read_text_cached(migration)
    for required in ("database_release_audit", "start_release_audit",
                     "complete_release_audit", "get_release_audit_history",
                     "schema_hash", "migration_set"):
        if required not in text:
            errors.append(f"Release audit migration 048 is missing: {required}")

def check_roll_forward(root: Path, errors: list[str]) -> None:
    """Verify roll forward migration (049) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "049_roll_forward.sql"
    if not migration.is_file():
        errors.append("Roll forward migration 049 is missing")
        return
    text = read_text_cached(migration)
    for required in ("roll_forward_migration", "record_roll_forward",
                     "verify_roll_forward", "get_roll_forwards_for",
                     "fixes_migration_id", "corrective_sql"):
        if required not in text:
            errors.append(f"Roll forward migration 049 is missing: {required}")

def check_workload_pool_query_class(root: Path, errors: list[str]) -> None:
    """Verify workload pool + query class migration (032) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "032_workload_pool_query_class.sql"
    if not migration.is_file():
        errors.append("Workload pool/query class migration 032 is missing")
        return
    text = read_text_cached(migration)
    for required in ("workload_pool_config", "query_class", "apply_query_class",
                     "index", "transport", "audit", "reconcile", "maintenance",
                     "interactive", "index_lookup", "audit_write",
                     "reconcile", "migration"):
        if required not in text:
            errors.append(f"Workload pool/query class migration 032 is missing: {required}")
