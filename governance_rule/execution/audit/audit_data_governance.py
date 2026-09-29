"""Data-governance migration and permission-snapshot audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_data_lineage(root: Path, errors: list[str]) -> None:
    """Verify data lineage migration (018) defines required objects."""
    lineage_migration = root / "shared-layer" / "migrations" / "018_data_lineage.sql"
    if not lineage_migration.is_file():
        errors.append("Data lineage migration 018 is missing")
        return
    text = read_text_cached(lineage_migration)
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
    text = read_text_cached(authority_migration)
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
    text = read_text_cached(provenance_migration)
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
    text = read_text_cached(helper)
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
    text = read_text_cached(migration)
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
    text = read_text_cached(migration)
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
    text = read_text_cached(migration)
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
    text = read_text_cached(migration)
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
    text = read_text_cached(helper)
    if "capture_snapshot" not in text:
        errors.append("Permission snapshot helper is missing: capture_snapshot")

def check_workload_class(root: Path, errors: list[str]) -> None:
    """Verify workload class migration (026) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "026_workload_class.sql"
    if not migration.is_file():
        errors.append("Workload class migration 026 is missing")
        return
    text = read_text_cached(migration)
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
    text = read_text_cached(migration)
    for required in ("deletion_stage", "tombstone", "retention", "purged",
                     "tombstone_resource", "advance_deletion_stage",
                     "get_purge_eligible", "purge_after"):
        if required not in text:
            errors.append(f"Two-stage deletion migration 027 is missing: {required}")
