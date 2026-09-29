"""Migration-113 SQL contract — every statement is fully parameterized."""
from __future__ import annotations


# ---------------------------------------------------------------------------
# SQL contract (migration 113) — every statement is fully parameterized.
# ---------------------------------------------------------------------------

OPERATION_INSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation "
    "(operation_id, operation_type, module_id, resource_id, generation, status, "
    "current_step, idempotency_key, correlation_id, fingerprint, checkpoint, "
    "created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, now(), now()) "
    "ON CONFLICT DO NOTHING RETURNING operation_id"
)

OPERATION_LOAD_SQL = (
    "SELECT operation_id, operation_type, module_id, resource_id, generation, status, "
    "current_step, idempotency_key, correlation_id, fingerprint, claimed_by, "
    "claimed_at, lease_until, worker_generation, checkpoint, created_at, updated_at, "
    "completed_at FROM gptbridge_workflow.operation WHERE operation_id = %s"
)

OPERATION_LOAD_BY_KEY_SQL = (
    "SELECT operation_id FROM gptbridge_workflow.operation WHERE idempotency_key = %s "
    "ORDER BY created_at LIMIT 1"
)

OPERATION_LOAD_BY_FINGERPRINT_SQL = (
    "SELECT operation_id FROM gptbridge_workflow.operation "
    "WHERE module_id = %s AND operation_type = %s AND fingerprint = %s "
    "ORDER BY created_at LIMIT 1"
)

OPERATION_CLAIM_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET status = 'RUNNING', claimed_by = %s, claimed_at = now(), "
    "lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "WHERE operation_id = %s "
    "AND status IN ('PENDING', 'RUNNING', 'REQUIRES_RECONCILE') "
    "AND (lease_until IS NULL OR lease_until < now()) "
    "RETURNING operation_id"
)

# Existing migration function: claims PENDING / RUNNING / REQUIRES_RECONCILE.
OPERATION_CLAIM_NEXT_SQL = (
    "SELECT gptbridge_workflow.claim_next_operation(%s, %s) AS operation_id"
)

# Reconcile claim: same lease discipline, but the operation stays parked in
# REQUIRES_RECONCILE until the callback's verdict moves it.
RECONCILE_CLAIM_SQL = (
    "WITH candidate AS ("
    "SELECT operation_id FROM gptbridge_workflow.operation "
    "WHERE status = 'REQUIRES_RECONCILE' "
    "AND (lease_until IS NULL OR lease_until < now()) "
    "ORDER BY updated_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    ") "
    "UPDATE gptbridge_workflow.operation target "
    "SET claimed_by = %s, claimed_at = now(), "
    "lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "FROM candidate WHERE target.operation_id = candidate.operation_id "
    "RETURNING target.operation_id"
)

OPERATION_HEARTBEAT_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "WHERE operation_id = %s AND claimed_by = %s AND status = 'RUNNING' "
    "RETURNING operation_id"
)

OPERATION_SAVE_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET status = %s, current_step = %s, checkpoint = %s, worker_generation = %s, "
    "claimed_by = %s, claimed_at = to_timestamp(NULLIF(%s, 0)), "
    "lease_until = to_timestamp(NULLIF(%s, 0)), updated_at = now(), "
    "completed_at = to_timestamp(NULLIF(%s, 0)) "
    "WHERE operation_id = %s"
)

OPERATION_STEP_UPSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation_step "
    "(operation_id, step_id, step_order, engine, action_type, status, "
    "attempt_count, started_at, completed_at, result_hash, error_code) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s) "
    "ON CONFLICT (operation_id, step_id) DO UPDATE SET "
    "status = excluded.status, attempt_count = excluded.attempt_count, "
    "completed_at = excluded.completed_at, result_hash = excluded.result_hash, "
    "error_code = excluded.error_code"
)

OPERATION_STEP_LIST_SQL = (
    "SELECT step_id, step_order, engine, action_type, status, attempt_count, "
    "result_hash, error_code FROM gptbridge_workflow.operation_step "
    "WHERE operation_id = %s ORDER BY step_order, step_id"
)

# Diagnostic listing (read-only): newest-first bounded summary projection.
OPERATION_LIST_SQL = (
    "SELECT operation_id, operation_type, module_id, resource_id, generation, status, "
    "current_step, correlation_id, created_at, updated_at, completed_at "
    "FROM gptbridge_workflow.operation ORDER BY created_at DESC, operation_id LIMIT %s"
)

OPERATION_EVENT_INSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation_event "
    "(operation_id, event_type, step_id, detail, created_at) "
    "VALUES (%s, %s, %s, %s, now())"
)
