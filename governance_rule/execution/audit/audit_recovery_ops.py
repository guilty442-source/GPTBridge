"""Recovery safety fence, startup/shutdown phase audit checks."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_recovery_safety_fence(root: Path, errors: list[str]) -> None:
    """Verify recovery safety fence migration (110)."""
    migration = root / "shared-layer" / "migrations" / "110_recovery_safety_fence.sql"
    if not migration.is_file():
        errors.append("Recovery safety fence migration 110 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_safety_fence", "check_safety_fence",
                     "record_fence_block", "drop_authoritative_database",
                     "truncate_official_data", "rewrite_governance_codex",
                     "change_rls_policy", "grant_elevated_role"):
        if required not in text:
            errors.append(f"Recovery safety fence migration 110 is missing: {required}")

def check_chaos_drill(root: Path, errors: list[str]) -> None:
    """Verify chaos drill migration (111)."""
    migration = root / "shared-layer" / "migrations" / "111_chaos_drill.sql"
    if not migration.is_file():
        errors.append("Chaos drill migration 111 is missing")
        return
    text = read_text_cached(migration)
    for required in ("chaos_drill", "start_chaos_drill", "complete_chaos_drill",
                     "no_authority_inversion", "no_duplicate_write",
                     "no_lost_commit", "no_silent_conflict",
                     "no_uncontrolled_retry"):
        if required not in text:
            errors.append(f"Chaos drill migration 111 is missing: {required}")

def check_recovery_certification(root: Path, errors: list[str]) -> None:
    """Verify recovery certification migration (112)."""
    migration = root / "shared-layer" / "migrations" / "112_recovery_certification.sql"
    if not migration.is_file():
        errors.append("Recovery certification migration 112 is missing")
        return
    text = read_text_cached(migration)
    for required in ("recovery_certification", "record_recovery_certification",
                     "certify_recovery_plan_v2", "is_recovery_plan_certified",
                     "integrity_result", "reconcile_result", "audit_result",
                     "certification_status", "CERTIFIED"):
        if required not in text:
            errors.append(f"Recovery certification migration 112 is missing: {required}")

def check_startup_phase(root: Path, errors: list[str]) -> None:
    """Verify startup phase migration (116)."""
    migration = root / "shared-layer" / "migrations" / "116_startup_phase.sql"
    if not migration.is_file():
        errors.append("Startup phase migration 116 is missing")
        return
    text = read_text_cached(migration)
    for required in ("startup_phase", "BOOTSTRAP", "GOVERNANCE_VALIDATED",
                     "DATABASE_FOUNDATION_READY", "CENTRAL_AUTHORITY_READY",
                     "PRIVATE_STATE_READY", "SEMANTIC_INDEX_READY",
                     "RECOVERY_READY", "READ_MODELS_READY", "CORE_READY",
                     "get_startup_order"):
        if required not in text:
            errors.append(f"Startup phase migration 116 is missing: {required}")

def check_startup_phase_gate(root: Path, errors: list[str]) -> None:
    """Verify startup phase gate migration (117)."""
    migration = root / "shared-layer" / "migrations" / "117_startup_phase_gate.sql"
    if not migration.is_file():
        errors.append("Startup phase gate migration 117 is missing")
        return
    text = read_text_cached(migration)
    for required in ("startup_phase_gate", "register_startup_gate",
                     "set_gate_result", "is_phase_complete",
                     "can_enable_write", "governance_ready",
                     "security_ready", "authority_ready", "audit_ready"):
        if required not in text:
            errors.append(f"Startup phase gate migration 117 is missing: {required}")

def check_schema_readiness(root: Path, errors: list[str]) -> None:
    """Verify schema readiness migration (118)."""
    migration = root / "shared-layer" / "migrations" / "118_schema_readiness.sql"
    if not migration.is_file():
        errors.append("Schema readiness migration 118 is missing")
        return
    text = read_text_cached(migration)
    for required in ("schema_readiness", "set_schema_readiness",
                     "is_schema_ready", "is_pg_certified",
                     "is_audit_writable", "can_enable_business_write",
                     "gptbridge_index", "gptbridge_transport",
                     "gptbridge_audit", "gptbridge_rag", "gptbridge_identity"):
        if required not in text:
            errors.append(f"Schema readiness migration 118 is missing: {required}")

def check_rag_readiness_gate(root: Path, errors: list[str]) -> None:
    """Verify RAG readiness gate migration (119)."""
    migration = root / "shared-layer" / "migrations" / "119_rag_readiness_gate.sql"
    if not migration.is_file():
        errors.append("RAG readiness gate migration 119 is missing")
        return
    text = read_text_cached(migration)
    for required in ("rag_readiness_gate", "evaluate_rag_readiness",
                     "is_rag_ready", "pg_rag_metadata_ready",
                     "vector_ready", "metadata_authority_wired",
                     "collection_contract_valid"):
        if required not in text:
            errors.append(f"RAG readiness gate migration 119 is missing: {required}")

def check_shutdown_phase(root: Path, errors: list[str]) -> None:
    """Verify shutdown phase migration (120)."""
    migration = root / "shared-layer" / "migrations" / "120_shutdown_phase.sql"
    if not migration.is_file():
        errors.append("Shutdown phase migration 120 is missing")
        return
    text = read_text_cached(migration)
    for required in ("shutdown_phase", "STOP_ACCEPTING_NEW_WORK",
                     "DRAIN_TRANSPORT", "FLUSH_AUDIT",
                     "CLOSE_VECTOR_CLIENT", "CLOSE_MODULE_PRIVATE_POOLS",
                     "CLOSE_POSTGRES_POOLS", "get_shutdown_order"):
        if required not in text:
            errors.append(f"Shutdown phase migration 120 is missing: {required}")

def check_shutdown_audit(root: Path, errors: list[str]) -> None:
    """Verify shutdown audit migration (121)."""
    migration = root / "shared-layer" / "migrations" / "121_shutdown_audit.sql"
    if not migration.is_file():
        errors.append("Shutdown audit migration 121 is missing")
        return
    text = read_text_cached(migration)
    for required in ("shutdown_audit", "start_shutdown_audit",
                     "complete_shutdown_audit",
                     "was_last_shutdown_graceful",
                     "shutdown_status", "graceful", "unclean"):
        if required not in text:
            errors.append(f"Shutdown audit migration 121 is missing: {required}")

def check_unclean_shutdown_detection(root: Path, errors: list[str]) -> None:
    """Verify unclean shutdown detection migration (122)."""
    migration = root / "shared-layer" / "migrations" / "122_unclean_shutdown_detection.sql"
    if not migration.is_file():
        errors.append("Unclean shutdown detection migration 122 is missing")
        return
    text = read_text_cached(migration)
    for required in ("unclean_shutdown_detection", "detect_unclean_shutdown",
                     "mark_unclean_step_done",
                     "is_unclean_recovery_complete",
                     "transport_lease_recovery",
                     "unknown_commit_verification"):
        if required not in text:
            errors.append(f"Unclean shutdown detection migration 122 is missing: {required}")

def check_cache_invalidation_policy(root: Path, errors: list[str]) -> None:
    """Verify cache invalidation policy migration (123)."""
    migration = root / "shared-layer" / "migrations" / "123_cache_invalidation_policy.sql"
    if not migration.is_file():
        errors.append("Cache invalidation policy migration 123 is missing")
        return
    text = read_text_cached(migration)
    for required in ("cache_invalidation_policy", "register_cache_policy",
                     "should_invalidate_cache",
                     "check_generation_compatible",
                     "check_revision_compatible", "check_ttl_valid"):
        if required not in text:
            errors.append(f"Cache invalidation policy migration 123 is missing: {required}")

def check_dependency_graph(root: Path, errors: list[str]) -> None:
    """Verify dependency graph migration (124)."""
    migration = root / "shared-layer" / "migrations" / "124_data_layer_dependency_graph.sql"
    if not migration.is_file():
        errors.append("Dependency graph migration 124 is missing")
        return
    text = read_text_cached(migration)
    for required in ("data_layer_dependency_graph", "get_dependencies",
                     "get_dependents", "governance_codex",
                     "identity_permission", "postgresql",
                     "structured_authority", "semantic_canonical",
                     "bounded_local_state", "non_canonical"):
        if required not in text:
            errors.append(f"Dependency graph migration 124 is missing: {required}")
