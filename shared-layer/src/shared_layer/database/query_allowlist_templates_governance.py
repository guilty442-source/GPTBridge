"""Allowlisted query templates: governance domains (version locks, recovery, data-layer, read models)."""
from __future__ import annotations


# ============================================================================
# Query templates — each key maps to a parameterized SQL string.
# Parameters use %s (psycopg) or named :name (psycopg named).
# ============================================================================

TEMPLATES_GOVERNANCE: dict[str, str] = {
    # --- Fail-closed (migration 073) ---
    "fail_closed.active": (
        "SELECT action_id, trigger_type, action_taken, domain, triggered_at "
        "FROM gptbridge_index.get_active_fail_closed(%s)"
    ),

    # --- Version lock (migration 074) ---
    "version_lock.list": (
        "SELECT component, version_string, major_version, minor_version, "
        "patch_version, locked_at FROM gptbridge_index.version_lock "
        "WHERE release_id = %s ORDER BY component"
    ),

    # --- Compatibility matrix ext (migration 075) ---
    "compat_matrix_ext.list": (
        "SELECT postgresql_version, psycopg_version, "
        "vectord_server_version, status, tested_at "
        "FROM gptbridge_index.compatibility_matrix_ext "
        "WHERE release_id = %s ORDER BY updated_at DESC"
    ),
    "compat_matrix_ext.forbidden": (
        "SELECT postgresql_version, psycopg_version, "
        "vectord_server_version, notes "
        "FROM gptbridge_index.get_forbidden_combinations()"
    ),

    # --- Upgrade classification (migration 076) ---
    "upgrade_classification.list": (
        "SELECT component, from_version, to_version, upgrade_class, "
        "required_validation, allows_unattended, classified_at "
        "FROM gptbridge_index.upgrade_classification "
        "ORDER BY classified_at DESC LIMIT %s"
    ),

    # --- Driver compatibility test (migration 077) ---
    "driver_compat.failed": (
        "SELECT test_id, driver_name, driver_version, test_category, "
        "failure_reason, tested_at "
        "FROM gptbridge_index.get_failed_driver_tests(NULL, %s)"
    ),

    # --- PG major upgrade rehearsal (migration 078) ---
    "pg_rehearsal.list": (
        "SELECT rehearsal_id, from_version, to_version, status, "
        "started_at, completed_at "
        "FROM gptbridge_index.pg_major_upgrade_rehearsal "
        "ORDER BY started_at DESC LIMIT %s"
    ),

    # --- SBOM (migration 081) ---
    "sbom.list": (
        "SELECT component, component_type, version, source, source_hash, "
        "install_path, verified FROM gptbridge_index.sbom_dependency_inventory "
        "WHERE release_id = %s ORDER BY component_type, component"
    ),

    # --- Vulnerability risk (migration 082) ---
    "vulnerability.critical": (
        "SELECT vuln_id, component, affected_versions, fixed_version, "
        "cve_id, risk_level, recommended_action, upgrade_deadline_days "
        "FROM gptbridge_index.get_critical_vulnerabilities()"
    ),

    # --- Dependency drift (migration 083) ---
    "dependency_drift.unverified": (
        "SELECT drift_id, component, expected_version, installed_version, "
        "drift_status, detected_at "
        "FROM gptbridge_index.get_unverified_dependencies()"
    ),

    # --- Offline bundle (migration 084) ---
    "offline_bundle.list": (
        "SELECT bundle_id, component, version, package_type, platform, "
        "file_hash, verified FROM gptbridge_index.offline_bundle "
        "WHERE release_id = %s ORDER BY component, version"
    ),

    # --- Release signature (migration 085) ---
    "release_signature.latest": (
        "SELECT signature_id, bundle_hash, component_count, "
        "tamper_state, signed_at "
        "FROM gptbridge_index.get_latest_signature(%s)"
    ),

    # --- Recovery plan (migration 098) ---
    "recovery_plan.active": (
        "SELECT plan_id, version, steps, verification_rules, required_authority "
        "FROM gptbridge_index.get_active_recovery_plan(%s)"
    ),

    # --- Recovery incident (migration 095) ---
    "recovery_incident.active": (
        "SELECT incident_id, incident_type, severity, status, detected_at "
        "FROM gptbridge_index.get_active_incidents()"
    ),

    # --- Recovery state machine (migration 098) ---
    "recovery_state.current": (
        "SELECT gptbridge_index.get_current_recovery_state(%s)"
    ),

    # --- PG offline recovery (migration 095) ---
    "pg_offline_recovery.list": (
        "SELECT recovery_id, fallback_status, confirmation_attempts, "
        "degraded_at, recovered_at FROM gptbridge_index.pg_offline_recovery "
        "ORDER BY failure_detected_at DESC LIMIT %s"
    ),

    # --- PG recovery verification (migration 098) ---
    "pg_recovery_verify.list": (
        "SELECT verification_id, overall_recoverable, failure_reason, verified_at "
        "FROM gptbridge_index.pg_recovery_verification "
        "WHERE incident_id = %s ORDER BY verified_at DESC"
    ),

    # --- Reconcile recovery (migration 095) ---
    "reconcile_recovery.list": (
        "SELECT phase_id, status, pending_snapshot_count, reconciled_count, "
        "conflict_count, verified_count "
        "FROM gptbridge_index.reconcile_recovery_phase "
        "WHERE incident_id = %s ORDER BY started_at DESC"
    ),

    # --- Recovery generation (migration 098) ---
    "recovery_generation.current": (
        "SELECT gptbridge_index.get_current_generation()"
    ),

    # --- Recovery barrier (migration 095) ---
    "recovery_barrier.active": (
        "SELECT barrier_id, barrier_type, raised_at, barrier_active "
        "FROM gptbridge_index.recovery_barrier "
        "WHERE barrier_active = true ORDER BY raised_at DESC"
    ),

    # --- Transport recovery (migration 098) ---
    "transport_recovery.unknown": (
        "SELECT recovery_id, idempotency_key, request_id, commit_state, detected_at "
        "FROM gptbridge_index.get_unknown_commits(%s)"
    ),

    # --- Lease recovery (migration 098) ---
    "lease_recovery.expired": (
        "SELECT lease_recovery_id, request_id, lease_until, lease_status "
        "FROM gptbridge_index.get_expired_leases(%s)"
    ),

    # --- Chaos drill (migration 111) ---
    "chaos_drill.list": (
        "SELECT drill_id, scenario_code, scenario_name, status, overall_passed "
        "FROM gptbridge_index.chaos_drill "
        "ORDER BY started_at DESC LIMIT %s"
    ),

    # --- Recovery certification (migration 112) ---
    "recovery_certification.list": (
        "SELECT certification_id, plan_id, certification_status, "
        "integrity_result, reconcile_result, audit_result "
        "FROM gptbridge_index.recovery_certification "
        "ORDER BY created_at DESC LIMIT %s"
    ),

    # --- Data layer contract (migration 114) ---
    "data_layer_contract.active": (
        "SELECT contract_id, contract_version, database_release_id, "
        "postgresql_schema_version, "
        "vector_contract_version, security_generation, data_generation "
        "FROM gptbridge_index.get_active_data_layer_contract()"
    ),

    # --- Dependency classification (migration 115) ---
    "dependency_classification.list": (
        "SELECT component_name, dependency_type, component_category, "
        "failure_effect, criticality "
        "FROM gptbridge_index.dependency_classification "
        "ORDER BY dependency_type, component_name"
    ),

    # --- Startup phase (migration 116) ---
    "startup_phase.order": (
        "SELECT phase_number, phase_name, required_components, "
        "write_enabled, can_accept_requests "
        "FROM gptbridge_index.startup_phase ORDER BY phase_number"
    ),

    # --- Startup phase gate (migration 117) ---
    "startup_gate.list": (
        "SELECT gate_id, phase_number, gate_name, gate_type, "
        "passed, required_for_write "
        "FROM gptbridge_index.startup_phase_gate "
        "WHERE phase_number = %s ORDER BY gate_name"
    ),

    # --- Schema readiness (migration 118) ---
    "schema_readiness.list": (
        "SELECT schema_name, ready, schema_version, migration_head, "
        "rls_enabled, force_rls "
        "FROM gptbridge_index.schema_readiness ORDER BY schema_name"
    ),

    # --- RAG readiness gate (migration 119) ---
    "rag_readiness.latest": (
        "SELECT rag_ready, pg_rag_metadata_ready, vector_ready, "
        "metadata_authority_wired, collection_contract_valid "
        "FROM gptbridge_index.rag_readiness_gate "
        "ORDER BY checked_at DESC LIMIT 1"
    ),

    # --- Shutdown phase (migration 120) ---
    "shutdown_phase.order": (
        "SELECT phase_number, phase_name, actions, timeout_seconds "
        "FROM gptbridge_index.shutdown_phase ORDER BY phase_number"
    ),

    # --- Shutdown audit (migration 121) ---
    "shutdown_audit.latest": (
        "SELECT shutdown_id, shutdown_status, started_at, completed_at, "
        "drain_result, pending_operations "
        "FROM gptbridge_index.shutdown_audit "
        "ORDER BY started_at DESC LIMIT %s"
    ),

    # --- Unclean shutdown detection (migration 122) ---
    "unclean_shutdown.latest": (
        "SELECT detection_id, was_graceful, extra_recovery_steps, "
        "steps_completed, all_steps_done "
        "FROM gptbridge_index.unclean_shutdown_detection "
        "ORDER BY detected_at DESC LIMIT 1"
    ),

    # --- Cache invalidation policy (migration 123) ---
    "cache_invalidation_policy.list": (
        "SELECT cache_name, check_generation_compatible, "
        "check_revision_compatible, check_ttl_valid, ttl_seconds, on_mismatch "
        "FROM gptbridge_index.cache_invalidation_policy"
    ),

    # --- Dependency graph (migration 124) ---
    "dependency_graph.edges": (
        "SELECT from_component, to_component, edge_type, boundary "
        "FROM gptbridge_index.data_layer_dependency_graph "
        "ORDER BY from_component, to_component"
    ),

    # --- Integration rules (migration 125) ---
    "integration_rule.list": (
        "SELECT rule_number, rule_text, rule_category, violation_effect "
        "FROM gptbridge_index.integration_rule "
        "WHERE active = true ORDER BY rule_number"
    ),

    # --- Read models / CQRS boundary (migration 087) ---
    # Active-version rows.  filter param semantics:
    #   resource_summary/module/rag -> module_id (or resource_type / state)
    #   transport                  -> state
    # NULL filter returns all rows (state NULL matches nothing, so the
    # `OR %s IS NULL` term is required and kept symmetric across projections).
    "readmodel.rows.resource_summary": (
        "SELECT r.module_id, r.resource_type, r.resource_count, "
        "r.total_revisions, r.distinct_hash_count, r.index_pending_count, "
        "r.last_activity_at, r.source_revision, r.projection_version, r.updated_at "
        "FROM gptbridge_readmodel.resource_summary r "
        "JOIN gptbridge_readmodel.projection_version pv "
        "  ON pv.projection_name = %s AND pv.state = 'active' "
        " AND pv.projection_version = r.projection_version "
        "WHERE (r.module_id = %s OR %s IS NULL) "
        "ORDER BY r.module_id, r.resource_type"
    ),
    "readmodel.rows.module_status_summary": (
        "SELECT m.module_id, m.resource_count, m.transport_queued_count, "
        "m.transport_pushed_count, m.transport_claimed_count, "
        "m.transport_completed_count, m.last_resource_activity, "
        "m.last_transport_activity, m.healthy, m.source_revision, "
        "m.projection_version, m.updated_at "
        "FROM gptbridge_readmodel.module_status_summary m "
        "JOIN gptbridge_readmodel.projection_version pv "
        "  ON pv.projection_name = %s AND pv.state = 'active' "
        " AND pv.projection_version = m.projection_version "
        "WHERE (m.module_id = %s OR %s IS NULL) "
        "ORDER BY m.module_id"
    ),
    "readmodel.rows.transport_status_summary": (
        "SELECT t.state, t.count, t.oldest_created_at, t.newest_created_at, "
        "t.last_activity_at, t.source_revision, t.projection_version, t.updated_at "
        "FROM gptbridge_readmodel.transport_status_summary t "
        "JOIN gptbridge_readmodel.projection_version pv "
        "  ON pv.projection_name = %s AND pv.state = 'active' "
        " AND pv.projection_version = t.projection_version "
        "WHERE (t.state = %s OR %s IS NULL) "
        "ORDER BY t.state"
    ),
    "readmodel.rows.rag_status_summary": (
        "SELECT g.module_id, g.chunk_count, g.resource_version_count, "
        "g.index_state_active_count, g.outbox_pending_count, g.outbox_failed_count, "
        "g.embedding_model, g.last_indexed_at, g.source_revision, "
        "g.projection_version, g.updated_at "
        "FROM gptbridge_readmodel.rag_status_summary g "
        "JOIN gptbridge_readmodel.projection_version pv "
        "  ON pv.projection_name = %s AND pv.state = 'active' "
        " AND pv.projection_version = g.projection_version "
        "WHERE (g.module_id = %s OR %s IS NULL) "
        "ORDER BY g.module_id"
    ),
    "readmodel.rows.database_status_snapshot": (
        "SELECT d.projection_version, d.workspace, d.pg_version, d.db_size_bytes, "
        "d.schema_count, d.table_count, d.rls_enforced_table_count, "
        "d.resource_count, d.transport_count, d.audit_last_24h_count, "
        "d.chunk_count, d.lineage_node_count, d.source_revision, d.updated_at "
        "FROM gptbridge_readmodel.database_status_snapshot d "
        "JOIN gptbridge_readmodel.projection_version pv "
        "  ON pv.projection_name = %s AND pv.state = 'active' "
        " AND pv.projection_version = d.projection_version "
        "ORDER BY d.projection_version DESC LIMIT 1"
    ),
    "readmodel.lag": (
        "SELECT projection_name, projection_revision, authority_revision, "
        "revision_lag, lag_seconds, watermark_unit, status, refreshed_at "
        "FROM gptbridge_readmodel.get_projection_lag(%s)"
    ),
    "readmodel.refresh": (
        "SELECT gptbridge_readmodel.refresh_projection(%s)"
    ),
    "readmodel.start_build": (
        "SELECT gptbridge_readmodel.start_projection_build(%s)"
    ),
    "readmodel.compute": (
        "SELECT gptbridge_readmodel.compute_projection_rows(%s, %s)"
    ),
    "readmodel.publish": (
        "SELECT gptbridge_readmodel.publish_projection_build(%s, %s)"
    ),
    "readmodel.drop": (
        "SELECT gptbridge_readmodel.drop_projection(%s)"
    ),
    "readmodel.watermark": (
        "SELECT gptbridge_readmodel.watermark_for(%s)"
    ),

    # --- RAG capacity governance (migration 088) ---
    "ragpolicy.current": (
        "SELECT gptbridge_ragpolicy.current_capacity_policy()"
    ),
    "ragpolicy.upsert": (
        "SELECT gptbridge_ragpolicy.upsert_capacity_policy("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "ragpolicy.queue.set": (
        "SELECT gptbridge_ragpolicy.set_queue_state(%s, %s, %s, %s)"
    ),
    "ragpolicy.queue.admit": (
        "SELECT gptbridge_ragpolicy.queue_admission(%s, %s, %s)"
    ),
    "ragpolicy.phase_latency.record": (
        "SELECT gptbridge_ragpolicy.record_phase_latency(%s, %s, %s)"
    ),
    "ragpolicy.trace.record": (
        "SELECT gptbridge_ragpolicy.record_retrieval_trace("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "ragpolicy.generation.init": (
        "SELECT gptbridge_ragpolicy.initialize_generation(%s, %s, %s, %s)"
    ),
    "ragpolicy.generation.advance": (
        "SELECT gptbridge_ragpolicy.advance_generation(%s, %s, %s)"
    ),
    "ragpolicy.slo.list": (
        "SELECT metric_name, target_value, direction, unit, description "
        "FROM gptbridge_ragpolicy.list_rag_slo()"
    ),
    "ragpolicy.queue.list": (
        "SELECT queue_name, state, threshold_depth, current_depth, reason, "
        "updated_at FROM gptbridge_ragpolicy.queue_state ORDER BY queue_name"
    ),
    "ragpolicy.generation.list": (
        "SELECT generation_id, logical_alias, physical_name, schema_version, "
        "embedding_model, embedding_dimension, state, initial_points, "
        "verification_result, alias_swapped_at, error_message, created_at, "
        "updated_at FROM gptbridge_ragpolicy.vector_collection_generation "
        "ORDER BY created_at DESC LIMIT %s"
    ),
    "ragpolicy.trace.list": (
        "SELECT trace_id, request_id, rag_type, round_no, query, "
        "rewritten_query, hit_ids, evidence_score, stop_reason, module_ids, "
        "generation_id, policy_version, created_at "
        "FROM gptbridge_ragpolicy.retrieval_trace "
        "WHERE request_id = %s ORDER BY round_no"
    ),
    "ragpolicy.latency.list": (
        "SELECT phase, elapsed_ms, created_at "
        "FROM gptbridge_ragpolicy.phase_latency "
        "WHERE request_id = %s ORDER BY phase"
    ),
}
