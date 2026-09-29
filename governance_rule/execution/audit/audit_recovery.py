"""Recovery plan, state machine and checkpoint audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_recovery_plan(root: Path, errors: list[str]) -> None:
    """Verify recovery plan migration (088) defines required objects."""
    migration = root / "shared-layer" / "migrations" / "088_recovery_plan.sql"
    if not migration.is_file():
        errors.append("Recovery plan migration 088 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_plan", "register_recovery_plan",
                     "certify_recovery_plan", "activate_recovery_plan",
                     "get_active_recovery_plan",
                     "incident_type", "steps", "verification_rules"):
        if required not in text:
            errors.append(f"Recovery plan migration 088 is missing: {required}")

def check_recovery_incident(root: Path, errors: list[str]) -> None:
    """Verify recovery incident migration (089)."""
    migration = root / "shared-layer" / "migrations" / "089_recovery_incident.sql"
    if not migration.is_file():
        errors.append("Recovery incident migration 089 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_incident", "open_recovery_incident",
                     "advance_incident_status", "get_active_incidents"):
        if required not in text:
            errors.append(f"Recovery incident migration 089 is missing: {required}")

def check_recovery_state_machine(root: Path, errors: list[str]) -> None:
    """Verify recovery state machine migration (090)."""
    migration = root / "shared-layer" / "migrations" / "090_recovery_state_machine.sql"
    if not migration.is_file():
        errors.append("Recovery state machine migration 090 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_state_machine", "recovery_state_transition",
                     "transition_recovery_state", "get_current_recovery_state",
                     "HEALTHY", "DEGRADED", "RECOVERING", "QUARANTINED"):
        if required not in text:
            errors.append(f"Recovery state machine migration 090 is missing: {required}")

def check_pg_offline_recovery(root: Path, errors: list[str]) -> None:
    """Verify PG offline recovery migration (091)."""
    migration = root / "shared-layer" / "migrations" / "091_pg_offline_recovery.sql"
    if not migration.is_file():
        errors.append("PG offline recovery migration 091 is missing")
        return
    text = read_text_cached(migration)
    for required in ("pg_offline_recovery", "confirm_pg_failure",
                     "enter_degraded_mode", "fallback_status"):
        if required not in text:
            errors.append(f"PG offline recovery migration 091 is missing: {required}")

def check_pg_recovery_verification(root: Path, errors: list[str]) -> None:
    """Verify PG recovery verification migration (092)."""
    migration = root / "shared-layer" / "migrations" / "092_pg_recovery_verification.sql"
    if not migration.is_file():
        errors.append("PG recovery verification migration 092 is missing")
        return
    text = read_text_cached(migration)
    for required in ("pg_recovery_verification", "record_pg_recovery_verification",
                     "is_pg_recoverable", "connection_ok", "schema_version_ok",
                     "rls_ok", "audit_ok", "transport_ok", "integrity_ok",
                     "generation_ok", "overall_recoverable"):
        if required not in text:
            errors.append(f"PG recovery verification migration 092 is missing: {required}")

def check_reconcile_recovery_phase(root: Path, errors: list[str]) -> None:
    """Verify reconcile recovery phase migration (093)."""
    migration = root / "shared-layer" / "migrations" / "093_reconcile_recovery_phase.sql"
    if not migration.is_file():
        errors.append("Reconcile recovery phase migration 093 is missing")
        return
    text = read_text_cached(migration)
    for required in ("reconcile_recovery_phase", "start_reconcile_recovery",
                     "advance_reconcile_recovery", "is_reconcile_complete"):
        if required not in text:
            errors.append(f"Reconcile recovery phase migration 093 is missing: {required}")

def check_recovery_generation(root: Path, errors: list[str]) -> None:
    """Verify recovery generation migration (094)."""
    migration = root / "shared-layer" / "migrations" / "094_recovery_generation.sql"
    if not migration.is_file():
        errors.append("Recovery generation migration 094 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_generation", "create_recovery_generation",
                     "get_current_generation", "degraded", "recovered"):
        if required not in text:
            errors.append(f"Recovery generation migration 094 is missing: {required}")

def check_recovery_barrier(root: Path, errors: list[str]) -> None:
    """Verify recovery barrier migration (095)."""
    migration = root / "shared-layer" / "migrations" / "095_recovery_barrier.sql"
    if not migration.is_file():
        errors.append("Recovery barrier migration 095 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_barrier", "raise_recovery_barrier",
                     "release_recovery_barrier", "is_recovery_barrier_active",
                     "RECOVERING_READ_ONLY"):
        if required not in text:
            errors.append(f"Recovery barrier migration 095 is missing: {required}")

def check_transport_recovery(root: Path, errors: list[str]) -> None:
    """Verify transport recovery migration (096)."""
    migration = root / "shared-layer" / "migrations" / "096_transport_recovery.sql"
    if not migration.is_file():
        errors.append("Transport recovery migration 096 is missing")
        return
    text = read_text_cached(migration)
    for required in ("transport_recovery", "register_unknown_commit",
                     "resolve_commit_state", "get_unknown_commits",
                     "idempotency_key", "COMMITTED", "NOT_COMMITTED", "UNKNOWN"):
        if required not in text:
            errors.append(f"Transport recovery migration 096 is missing: {required}")

def check_lease_recovery(root: Path, errors: list[str]) -> None:
    """Verify lease recovery migration (098)."""
    migration = root / "shared-layer" / "migrations" / "098_lease_recovery.sql"
    if not migration.is_file():
        errors.append("Lease recovery migration 098 is missing")
        return
    text = read_text_cached(migration)
    for required in ("lease_recovery", "check_lease_expiry",
                     "reclaim_lease", "get_expired_leases",
                     "lease_until", "worker_generation"):
        if required not in text:
            errors.append(f"Lease recovery migration 098 is missing: {required}")

def check_recovery_checkpoint(root: Path, errors: list[str]) -> None:
    """Verify recovery checkpoint migration (108)."""
    migration = root / "shared-layer" / "migrations" / "108_recovery_checkpoint.sql"
    if not migration.is_file():
        errors.append("Recovery checkpoint migration 108 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_checkpoint", "save_recovery_checkpoint",
                     "resume_recovery_checkpoint", "get_latest_checkpoint",
                     "current_phase", "last_processed_revision", "batch_cursor"):
        if required not in text:
            errors.append(f"Recovery checkpoint migration 108 is missing: {required}")

def check_recovery_idempotency(root: Path, errors: list[str]) -> None:
    """Verify recovery idempotency migration (109)."""
    migration = root / "shared-layer" / "migrations" / "109_recovery_idempotency.sql"
    if not migration.is_file():
        errors.append("Recovery idempotency migration 109 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_idempotency", "check_or_mark_idempotent",
                     "mark_idempotent_complete", "operation_key"):
        if required not in text:
            errors.append(f"Recovery idempotency migration 109 is missing: {required}")
