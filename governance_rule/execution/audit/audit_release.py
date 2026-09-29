"""Release, compatibility and dependency-inventory audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_compatibility_matrix_ext(root: Path, errors: list[str]) -> None:
    """Verify extended compatibility matrix migration (075)."""
    migration = root / "shared-layer" / "migrations" / "075_compatibility_matrix_ext.sql"
    if not migration.is_file():
        errors.append("Compatibility matrix ext migration 075 is missing")
        return
    text = read_text_cached(migration)
    for required in ("compatibility_matrix_ext", "record_compatibility",
                     "check_combination_allowed", "get_forbidden_combinations",
                     "postgresql_version", "psycopg_version"):
        if required not in text:
            errors.append(f"Compatibility matrix ext migration 075 is missing: {required}")

def check_upgrade_classification(root: Path, errors: list[str]) -> None:
    """Verify upgrade classification migration (076)."""
    migration = root / "shared-layer" / "migrations" / "076_upgrade_classification.sql"
    if not migration.is_file():
        errors.append("Upgrade classification migration 076 is missing")
        return
    text = read_text_cached(migration)
    for required in ("upgrade_classification", "classify_upgrade",
                     "get_upgrade_class", "patch", "minor", "major",
                     "required_validation", "allows_unattended"):
        if required not in text:
            errors.append(f"Upgrade classification migration 076 is missing: {required}")

def check_driver_compatibility_test(root: Path, errors: list[str]) -> None:
    """Verify driver compatibility test migration (077)."""
    migration = root / "shared-layer" / "migrations" / "077_driver_compatibility_test.sql"
    if not migration.is_file():
        errors.append("Driver compatibility test migration 077 is missing")
        return
    text = read_text_cached(migration)
    for required in ("driver_compatibility_test", "record_driver_test",
                     "is_driver_version_verified", "get_failed_driver_tests",
                     "connection_pool", "transaction", "row_factory",
                     "skip_locked"):
        if required not in text:
            errors.append(f"Driver compatibility test migration 077 is missing: {required}")

def check_pg_major_upgrade_rehearsal(root: Path, errors: list[str]) -> None:
    """Verify PG major upgrade rehearsal migration (078)."""
    migration = root / "shared-layer" / "migrations" / "078_pg_major_upgrade_rehearsal.sql"
    if not migration.is_file():
        errors.append("PG major upgrade rehearsal migration 078 is missing")
        return
    text = read_text_cached(migration)
    for required in ("pg_major_upgrade_rehearsal", "start_pg_rehearsal",
                     "advance_pg_rehearsal", "get_rehearsal_summary",
                     "migration_check", "rls_check", "transport_test",
                     "reconcile_test", "performance_baseline"):
        if required not in text:
            errors.append(f"PG major upgrade rehearsal migration 078 is missing: {required}")

def check_sbom_dependency_inventory(root: Path, errors: list[str]) -> None:
    """Verify SBOM dependency inventory migration (081)."""
    migration = root / "shared-layer" / "migrations" / "081_sbom_dependency_inventory.sql"
    if not migration.is_file():
        errors.append("SBOM dependency inventory migration 081 is missing")
        return
    text = read_text_cached(migration)
    for required in ("sbom_dependency_inventory", "record_sbom_entry",
                     "verify_sbom_entry", "get_sbom_for_release",
                     "component", "component_type", "version",
                     "source", "source_hash", "install_path"):
        if required not in text:
            errors.append(f"SBOM dependency inventory migration 081 is missing: {required}")

def check_vulnerability_risk(root: Path, errors: list[str]) -> None:
    """Verify vulnerability risk migration (082)."""
    migration = root / "shared-layer" / "migrations" / "082_vulnerability_risk.sql"
    if not migration.is_file():
        errors.append("Vulnerability risk migration 082 is missing")
        return
    text = read_text_cached(migration)
    for required in ("vulnerability_risk", "record_vulnerability",
                     "resolve_vulnerability", "get_critical_vulnerabilities",
                     "critical_security", "important",
                     "compatible_maintenance", "optional",
                     "immediate_upgrade", "scheduled_upgrade"):
        if required not in text:
            errors.append(f"Vulnerability risk migration 082 is missing: {required}")

def check_dependency_drift(root: Path, errors: list[str]) -> None:
    """Verify dependency drift migration (083)."""
    migration = root / "shared-layer" / "migrations" / "083_dependency_drift.sql"
    if not migration.is_file():
        errors.append("Dependency drift migration 083 is missing")
        return
    text = read_text_cached(migration)
    for required in ("dependency_drift", "record_dependency_drift",
                     "resolve_dependency_drift", "get_unverified_dependencies",
                     "UNVERIFIED_DEPENDENCY", "MISMATCH", "VERIFIED",
                     "expected_version", "installed_version"):
        if required not in text:
            errors.append(f"Dependency drift migration 083 is missing: {required}")

def check_offline_bundle(root: Path, errors: list[str]) -> None:
    """Verify offline bundle migration (084)."""
    migration = root / "shared-layer" / "migrations" / "084_offline_bundle.sql"
    if not migration.is_file():
        errors.append("Offline bundle migration 084 is missing")
        return
    text = read_text_cached(migration)
    for required in ("offline_bundle", "register_offline_bundle",
                     "verify_offline_bundle", "get_offline_bundle",
                     "component", "version", "package_type",
                     "storage_locator", "file_hash"):
        if required not in text:
            errors.append(f"Offline bundle migration 084 is missing: {required}")

def check_release_signature(root: Path, errors: list[str]) -> None:
    """Verify release signature migration (085)."""
    migration = root / "shared-layer" / "migrations" / "085_release_signature.sql"
    if not migration.is_file():
        errors.append("Release signature migration 085 is missing")
        return
    text = read_text_cached(migration)
    for required in ("release_signature", "sign_release",
                     "verify_release_signature", "get_latest_signature",
                     "bundle_hash", "component_count", "component_hashes",
                     "tamper_state"):
        if required not in text:
            errors.append(f"Release signature migration 085 is missing: {required}")

def check_database_release_manifest(root: Path, errors: list[str]) -> None:
    """Verify database release manifest migration (040) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "040_database_release_manifest.sql"
    if not migration.is_file():
        errors.append("Database release manifest migration 040 is missing")
        return
    text = read_text_cached(migration)
    for required in ("database_release", "transition_release_state",
                     "get_active_release", "supersede_active_release",
                     "DRAFT", "VALIDATED", "CERTIFIED", "STAGED",
                     "ACTIVE", "SUPERSEDED", "ARCHIVED", "REJECTED"):
        if required not in text:
            errors.append(f"Database release manifest migration 040 is missing: {required}")

def check_compatibility_matrix(root: Path, errors: list[str]) -> None:
    """Verify compatibility matrix migration (041) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "041_compatibility_matrix.sql"
    if not migration.is_file():
        errors.append("Compatibility matrix migration 041 is missing")
        return
    text = read_text_cached(migration)
    for required in ("release_compatibility", "check_compatibility",
                     "upsert_compatibility", "full", "read-only", "rejected"):
        if required not in text:
            errors.append(f"Compatibility matrix migration 041 is missing: {required}")

def check_migration_breaking_change(root: Path, errors: list[str]) -> None:
    """Verify migration breaking change migration (042) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "042_migration_breaking_change.sql"
    if not migration.is_file():
        errors.append("Migration breaking change migration 042 is missing")
        return
    text = read_text_cached(migration)
    for required in ("migration_classification", "classify_migration",
                     "get_breaking_migrations",
                     "compatible", "conditional", "breaking",
                     "pre_migration", "rollback_plan"):
        if required not in text:
            errors.append(f"Migration breaking change migration 042 is missing: {required}")
