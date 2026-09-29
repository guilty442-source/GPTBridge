"""Runtime module-presence audit checks (database subsystem)."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_orphan_scanner(root: Path, errors: list[str]) -> None:
    """Verify the runtime orphan scanner module exists."""
    scanner = root / "shared-layer" / "src" / "shared_layer" / "database" / "orphan_scanner.py"
    if not scanner.is_file():
        errors.append("Orphan scanner module is missing")
        return
    text = read_text_cached(scanner)
    if "scan_orphans" not in text:
        errors.append("Orphan scanner is missing: scan_orphans")

def check_deletion_coordinator(root: Path, errors: list[str]) -> None:
    """Verify the runtime deletion coordinator module exists."""
    coordinator = root / "shared-layer" / "src" / "shared_layer" / "database" / "deletion_coordinator.py"
    if not coordinator.is_file():
        errors.append("Deletion coordinator module is missing")
        return
    text = read_text_cached(coordinator)
    for required in ("tombstone", "advance_stage", "get_purge_eligible"):
        if required not in text:
            errors.append(f"Deletion coordinator is missing: {required}")

def check_generation_fence_helper(root: Path, errors: list[str]) -> None:
    """Verify the runtime generation fence helper module exists."""
    helper = root / "shared-layer" / "src" / "shared_layer" / "database" / "generation_fence.py"
    if not helper.is_file():
        errors.append("Generation fence helper module is missing")
        return
    text = read_text_cached(helper)
    for required in ("get_current_generation", "bump_generation", "is_connection_stale"):
        if required not in text:
            errors.append(f"Generation fence helper is missing: {required}")

def check_lifecycle_manager_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime lifecycle manager module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "lifecycle_manager.py"
    if not module.is_file():
        errors.append("Lifecycle manager module is missing")
        return
    text = read_text_cached(module)
    for required in ("transition_state", "get_state", "enqueue_purge",
                     "get_purge_eligible", "check_dependencies",
                     "record_purge", "place_hold", "release_hold",
                     "has_active_hold", "register_archive"):
        if required not in text:
            errors.append(f"Lifecycle manager module is missing: {required}")

def check_integrity_verifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime integrity verifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "integrity_verifier.py"
    if not module.is_file():
        errors.append("Integrity verifier module is missing")
        return
    text = read_text_cached(module)
    for required in ("compute_hash", "compute_merkle_root",
                     "populate_event_hash_chain", "verify_audit_chain",
                     "get_audit_head_hash", "start_reconcile_batch",
                     "complete_reconcile_batch", "record_resource_hash",
                     "verify_resource_hash",
                     "record_merkle_root", "create_integrity_snapshot",
                     "record_restore_verification", "record_tamper_state",
                     "trigger_fail_closed", "is_fail_closed_active"):
        if required not in text:
            errors.append(f"Integrity verifier module is missing: {required}")

def check_recovery_orchestrator_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime recovery orchestrator module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "recovery_orchestrator.py"
    if not module.is_file():
        errors.append("Recovery orchestrator module is missing")
        return
    text = read_text_cached(module)
    for required in ("register_recovery_plan", "open_recovery_incident",
                     "transition_recovery_state", "create_recovery_generation",
                     "get_current_generation", "start_pg_offline_recovery",
                     "confirm_pg_failure", "record_pg_recovery_verification",
                     "is_pg_recoverable", "register_unknown_commit",
                     "resolve_commit_state", "check_lease_expiry",
                     "reclaim_lease", "raise_recovery_barrier",
                     "is_recovery_barrier_active", "release_recovery_barrier",
                     "save_recovery_checkpoint", "check_or_mark_idempotent",
                     "mark_idempotent_complete", "check_safety_fence",
                     "record_fence_block", "is_recovery_plan_certified",
                     "start_chaos_drill", "complete_chaos_drill"):
        if required not in text:
            errors.append(f"Recovery orchestrator module is missing: {required}")

def check_data_layer_contract_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime data layer contract module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "data_layer_contract.py"
    if not module.is_file():
        errors.append("Data layer contract module is missing")
        return
    text = read_text_cached(module)
    for required in ("register_data_layer_contract", "activate_data_layer_contract",
                     "get_active_data_layer_contract", "classify_dependency",
                     "get_startup_order", "get_shutdown_order",
                     "register_startup_gate", "set_gate_result",
                     "is_phase_complete", "can_enable_write",
                     "set_schema_readiness", "is_schema_ready",
                     "is_pg_certified", "can_enable_business_write",
                     "evaluate_rag_readiness", "is_rag_ready",
                     "start_shutdown_audit", "complete_shutdown_audit",
                     "was_last_shutdown_graceful", "detect_unclean_shutdown",
                     "mark_unclean_step_done", "is_unclean_recovery_complete",
                     "register_cache_policy", "should_invalidate_cache",
                     "get_dependencies", "get_dependents",
                     "get_integration_rules", "check_integration_rule"):
        if required not in text:
            errors.append(f"Data layer contract module is missing: {required}")
