"""Recovery Orchestrator (Phase J).

Runtime helpers for recovery orchestration:
- Recovery plan versioning
- Incident tracking
- State machine transitions
- PG offline recovery
- PG recovery verification
- Reconcile recovery phase
- Recovery generation
- Recovery barrier
- Transport recovery
- Unknown commit resolution
- Lease recovery
- SQLite fallback freeze
- Recovery priority
- Qdrant recovery
- Qdrant full rebuild
- SQLite single-DB recovery
- Codex SQLite special recovery
- Backup restore orchestration
- PITR boundary
- Retry policy
- Checkpoint
- Idempotency
- Safety fence
- Chaos drill
- Recovery certification

Codex basis:
    A46/E22 — Audit: mandatory-ledger.
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def register_recovery_plan(
    conn: Any, plan_id: str, version: int, incident_type: str,
    steps: list[dict], **kwargs: Any,
) -> None:
    """Register a new recovery plan version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_recovery_plan(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (plan_id, version, incident_type, json.dumps(steps),
             json.dumps(kwargs.get("preconditions", [])),
             json.dumps(kwargs.get("timeouts", {})),
             kwargs.get("rollback_strategy"),
             json.dumps(kwargs.get("verification_rules", [])),
             kwargs.get("required_authority", "governance_rule")),
        )
    conn.commit()


def open_recovery_incident(
    conn: Any, plan_id: str, incident_type: str, detected_by: str,
    **kwargs: Any,
) -> str:
    """Open a recovery incident."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.open_recovery_incident(%s, %s, %s, %s, %s, %s, %s)",
            (plan_id, incident_type, detected_by,
             kwargs.get("description"),
             kwargs.get("affected_components", []),
             kwargs.get("severity", "minor")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def transition_recovery_state(
    conn: Any, incident_id: str, to_state: str,
    transitioned_by: str, reason: str | None = None,
) -> bool:
    """Transition recovery state (validates allowed transitions)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.transition_recovery_state(%s, %s, %s, %s)",
            (incident_id, to_state, transitioned_by, reason),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


def create_recovery_generation(
    conn: Any, incident_id: str, generation_type: str,
    description: str | None = None,
) -> int:
    """Create a new recovery generation."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.create_recovery_generation(%s, %s, %s)",
            (incident_id, generation_type, description),
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else 0


def get_current_generation(conn: Any) -> int:
    """Get current recovery generation number."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.get_current_generation()")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def start_pg_offline_recovery(conn: Any, incident_id: str) -> str:
    """Start PG offline recovery tracking."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.start_pg_offline_recovery(%s)",
            (incident_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def confirm_pg_failure(
    conn: Any, recovery_id: str, required_attempts: int = 3,
) -> bool:
    """Confirm PG failure after required attempts."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.confirm_pg_failure(%s, %s)",
            (recovery_id, required_attempts),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


def record_pg_recovery_verification(
    conn: Any, incident_id: str, verified_by: str,
    connection_ok: bool, schema_version_ok: bool,
    database_release_ok: bool, roles_ok: bool, rls_ok: bool,
    audit_ok: bool, transport_ok: bool, integrity_ok: bool,
    generation_ok: bool, failure_reason: str | None = None,
) -> str:
    """Record PG recovery verification result."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_pg_recovery_verification("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (incident_id, verified_by, connection_ok, schema_version_ok,
             database_release_ok, roles_ok, rls_ok, audit_ok, transport_ok,
             integrity_ok, generation_ok, failure_reason),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def is_pg_recoverable(conn: Any, incident_id: str) -> bool:
    """Check if PG is fully recoverable."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_pg_recoverable(%s)",
            (incident_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


@dataclass
class PgRecoveryVerificationResult:
    """Typed evidence of a drill-backed PG recovery verification."""

    drill: Any = None
    recorded: bool = False
    verification_id: str = ""
    flags: dict[str, bool] = field(default_factory=dict)
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded": self.recorded,
            "verification_id": self.verification_id,
            "flags": dict(self.flags),
            "failure_reason": self.failure_reason,
            "drill": (
                self.drill.to_dict()
                if hasattr(self.drill, "to_dict") else {}
            ),
        }


def _drill_flag(checks: dict[str, Any], name: str) -> bool:
    check = checks.get(name)
    return bool(check is not None and check.passed)


def verify_pg_recovery_with_drill(
    conn: Any,
    incident_id: str,
    verified_by: str,
    adapter: Any,
    work_dir: str | Path | None = None,
    *,
    record: bool = True,
    generation_check: Any | None = None,
) -> PgRecoveryVerificationResult:
    """Production entry: run a real restore drill, map evidence, record it.

    Evidence mapping is fail-closed: a missing or failed drill check maps to
    ``False`` — never inferred from an unrelated check.  ``generation_ok``
    requires an explicit generation probe (``generation_check`` callable or
    ``adapter.verify_generation``); without one it stays False and the
    verification is recorded as not recoverable.  Recording is skipped when
    ``conn`` is None; the typed result still reports ``recorded=False``.
    """
    from .restore_drill import run_restore_drill

    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="pg-recovery-drill-"))
    drill = run_restore_drill(adapter, Path(work_dir))
    checks = {check.name: check for check in drill.checks}

    generation_ok = False
    generation_detail = "not-verified:no-generation-probe"
    probe = generation_check or getattr(adapter, "verify_generation", None)
    if callable(probe):
        try:
            outcome = probe()
            generation_ok = bool(outcome[0])
            generation_detail = str(outcome[1])[:200]
        except Exception as exc:
            generation_ok = False
            generation_detail = f"error:{exc}"[:200]

    flags = {
        "connection_ok": bool(drill.engine_live),
        "schema_version_ok": _drill_flag(checks, "schema_version"),
        "database_release_ok": _drill_flag(checks, "migration_head"),
        "roles_ok": _drill_flag(checks, "rls_roles"),
        "rls_ok": _drill_flag(checks, "rls_roles"),
        "audit_ok": _drill_flag(checks, "audit_sequence"),
        "transport_ok": _drill_flag(checks, "transport_state"),
        "integrity_ok": all(_drill_flag(checks, name) for name in (
            "resource_count",
            "relation_count",
            "locator_integrity",
            "qdrant_references",
        )),
        "generation_ok": generation_ok,
    }
    reasons = [name for name, passed in flags.items() if not passed]
    if not generation_ok:
        reasons.append(generation_detail)
    failure_reason = (
        "drill-flags-not-passed:" + ",".join(reasons) if reasons else None
    )
    result = PgRecoveryVerificationResult(
        drill=drill,
        flags=flags,
        failure_reason=failure_reason,
    )
    if record and conn is not None:
        result.verification_id = record_pg_recovery_verification(
            conn, incident_id, verified_by, **flags,
            failure_reason=failure_reason,
        )
        result.recorded = bool(result.verification_id)
    return result


def register_unknown_commit(
    conn: Any, incident_id: str, idempotency_key: str,
    **kwargs: Any,
) -> str:
    """Register an unknown commit state for transport recovery."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_unknown_commit(%s, %s, %s, %s, %s)",
            (incident_id, idempotency_key,
             kwargs.get("request_id"),
             kwargs.get("operation_id"),
             kwargs.get("resource_revision")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def resolve_commit_state(
    conn: Any, recovery_id: str, resolved_state: str,
    resolution_method: str, notes: str | None = None,
) -> None:
    """Resolve an unknown commit state."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.resolve_commit_state(%s, %s, %s, %s)",
            (recovery_id, resolved_state, resolution_method, notes),
        )
    conn.commit()


def check_lease_expiry(
    conn: Any, incident_id: str, request_id: str,
    lease_until: str, **kwargs: Any,
) -> str:
    """Check if a transport lease has expired."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.check_lease_expiry(%s, %s, %s, %s, %s, %s)",
            (incident_id, request_id, lease_until,
             kwargs.get("original_worker"),
             kwargs.get("original_claimed_at"),
             kwargs.get("worker_generation")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def reclaim_lease(conn: Any, lease_recovery_id: str, reclaimed_by: str) -> None:
    """Reclaim an expired lease."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.reclaim_lease(%s, %s)",
            (lease_recovery_id, reclaimed_by),
        )
    conn.commit()


def raise_recovery_barrier(
    conn: Any, incident_id: str, barrier_type: str,
    raised_by: str, reason: str | None = None,
) -> str:
    """Raise a recovery barrier."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.raise_recovery_barrier(%s, %s, %s, %s)",
            (incident_id, barrier_type, raised_by, reason),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def is_recovery_barrier_active(conn: Any) -> bool:
    """Check if any recovery barrier is active."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.is_recovery_barrier_active()")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def release_recovery_barrier(
    conn: Any, barrier_id: str, released_by: str, release_reason: str,
) -> None:
    """Release a recovery barrier."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.release_recovery_barrier(%s, %s, %s)",
            (barrier_id, released_by, release_reason),
        )
    conn.commit()


def save_recovery_checkpoint(
    conn: Any, recovery_run_id: str, incident_id: str,
    current_phase: str, **kwargs: Any,
) -> str:
    """Save a recovery checkpoint for crash recovery."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.save_recovery_checkpoint(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (recovery_run_id, incident_id, current_phase,
             kwargs.get("last_completed_step"),
             kwargs.get("last_processed_revision"),
             kwargs.get("batch_cursor"),
             kwargs.get("generation"),
             kwargs.get("total_processed", 0),
             kwargs.get("total_failed", 0)),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def check_or_mark_idempotent(
    conn: Any, recovery_run_id: str, operation_key: str,
    operation_type: str,
) -> bool:
    """Check if operation already completed, or mark it as in-progress.
    Returns True if already completed (skip), False if newly marked (proceed).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.check_or_mark_idempotent(%s, %s, %s)",
            (recovery_run_id, operation_key, operation_type),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


def mark_idempotent_complete(
    conn: Any, recovery_run_id: str, operation_key: str,
    result: dict | None = None,
) -> None:
    """Mark an idempotent operation as complete."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.mark_idempotent_complete(%s, %s, %s)",
            (recovery_run_id, operation_key,
             json.dumps(result) if result else None),
        )
    conn.commit()


def check_safety_fence(conn: Any, action: str) -> bool:
    """Check if an action is forbidden by the safety fence.
    Returns True if FORBIDDEN (blocked).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.check_safety_fence(%s)",
            (action,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def record_fence_block(conn: Any, action: str) -> None:
    """Record that a safety fence block occurred."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_fence_block(%s)",
            (action,),
        )
    conn.commit()


def is_recovery_plan_certified(conn: Any, plan_id: str) -> bool:
    """Check if a recovery plan is certified."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_recovery_plan_certified(%s)",
            (plan_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def start_chaos_drill(
    conn: Any, scenario_code: str, plan_id: str | None = None,
) -> str:
    """Start a chaos drill scenario."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.start_chaos_drill(%s, %s)",
            (scenario_code, plan_id),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def complete_chaos_drill(
    conn: Any, drill_id: str,
    no_authority_inversion: bool, no_duplicate_write: bool,
    no_lost_commit: bool, no_silent_conflict: bool,
    no_uncontrolled_retry: bool, failure_reason: str | None = None,
) -> None:
    """Complete a chaos drill with verification gates."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.complete_chaos_drill(%s, %s, %s, %s, %s, %s, %s)",
            (drill_id, no_authority_inversion, no_duplicate_write,
             no_lost_commit, no_silent_conflict, no_uncontrolled_retry,
             failure_reason),
        )
    conn.commit()


__all__ = [
    "PgRecoveryVerificationResult",
    "register_recovery_plan",
    "open_recovery_incident",
    "transition_recovery_state",
    "create_recovery_generation",
    "get_current_generation",
    "start_pg_offline_recovery",
    "confirm_pg_failure",
    "record_pg_recovery_verification",
    "is_pg_recoverable",
    "register_unknown_commit",
    "resolve_commit_state",
    "check_lease_expiry",
    "reclaim_lease",
    "raise_recovery_barrier",
    "is_recovery_barrier_active",
    "release_recovery_barrier",
    "save_recovery_checkpoint",
    "check_or_mark_idempotent",
    "mark_idempotent_complete",
    "check_safety_fence",
    "record_fence_block",
    "is_recovery_plan_certified",
    "start_chaos_drill",
    "complete_chaos_drill",
    "verify_pg_recovery_with_drill",
]
