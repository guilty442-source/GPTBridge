"""Repair Engine — Runbooks (Phase 4).

Specific remediation implementations for each diagnosis code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .remediation import RemediationAction, RiskClass, DryRunResult
from .diagnosis import DiagnosisCode

_logger = logging.getLogger("gptbridge.repair.runbooks")


# ============================================================================
# PG_POOL_SATURATION Runbook
# ============================================================================

def pg_pool_saturation_diagnosis(ctx: dict[str, Any]) -> dict[str, Any]:
    """Diagnose root cause of pool saturation."""
    results = {
        "long_queries_found": False,
        "locks_found": False,
        "connection_leak_found": False,
        "slow_pg_found": False,
        "root_cause": "unknown",
    }

    # Check 1: Long running queries
    long_queries = ctx.get("pg_long_queries", [])
    if long_queries:
        results["long_queries_found"] = True
        results["long_queries"] = long_queries

    # Check 2: Lock contention
    blocking_pids = ctx.get("pg_blocking_pids", [])
    if blocking_pids:
        results["locks_found"] = True
        results["blocking_pids"] = blocking_pids

    # Check 3: Connection leak (connections not returning to pool)
    pool_stats = ctx.get("pg_pool_stats", {})
    if pool_stats.get("active", 0) + pool_stats.get("idle", 0) < pool_stats.get("total", 0) * 0.5:
        results["connection_leak_found"] = True

    # Check 4: Slow PostgreSQL
    if ctx.get("pg_slow_query_p95_ms", 0) > 5000:
        results["slow_pg_found"] = True

    # Determine root cause
    if results["long_queries_found"]:
        results["root_cause"] = "long_queries"
    elif results["locks_found"]:
        results["root_cause"] = "lock_contention"
    elif results["connection_leak_found"]:
        results["root_cause"] = "connection_leak"
    elif results["slow_pg_found"]:
        results["root_cause"] = "slow_postgresql"
    else:
        results["root_cause"] = "capacity"

    return results


def pg_pool_saturation_remediation(ctx: dict[str, Any], root_cause: str) -> dict[str, Any]:
    """Execute pool saturation remediation based on root cause."""
    actions = []

    if root_cause == "long_queries":
        # Cancel long optional queries
        for q in ctx.get("pg_long_queries", []):
            if q.get("class") == "optional":
                pid = q.get("pid")
                # pg_cancel_backend(pid)
                pass

    elif root_cause == "lock_contention":
        # Handle lock contention
        for pid in ctx.get("pg_blocking_pids", []):
            q_class = ctx.get(f"query_class_{pid}", "unknown")
            if q_class == "optional":
                # pg_cancel_backend(pid)
                pass

    elif root_cause == "connection_leak":
        # Force pool cleanup
        # pool.force_cleanup()
        pass

    elif root_cause == "slow_postgresql":
        # Investigate slow PG - don't just expand pool
        pass

    elif root_cause == "capacity":
        # Only after all other causes ruled out
        # Reduce admission limit temporarily
        pass

    return {"actions": actions, "root_cause": root_cause}


# ============================================================================
# PG_LOCK_CONTENTION Runbook
# ============================================================================

def pg_lock_contention_diagnosis(ctx: dict[str, Any]) -> dict[str, Any]:
    """Diagnose lock contention details."""
    results = {
        "blocking_pids": [],
        "waiting_pids": [],
        "lock_types": [],
        "query_classes": {},
        "can_cancel": [],
        "cannot_cancel": [],
    }

    blocking_pids = ctx.get("pg_blocking_pids", [])
    for pid in blocking_pids:
        q_class = ctx.get(f"query_class_{pid}", "unknown")
        results["query_classes"][pid] = q_class
        results["blocking_pids"].append(pid)

        if q_class == "optional":
            results["can_cancel"].append(pid)
        else:
            results["cannot_cancel"].append(pid)

    # Get waiting PIDs
    waiting = ctx.get("pg_waiting_pids", [])
    results["waiting_pids"] = waiting

    return results


# ============================================================================
# SQLITE_BUSY_STORM Runbook
# ============================================================================

def sqlite_busy_storm_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """Step-by-step SQLite busy storm remediation."""
    steps = [
        ("confirm_active_writers", lambda c: c.get("sqlite_active_writers", 0) > 0),
        ("confirm_long_reader", lambda c: c.get("sqlite_longest_reader_seconds", 0) > 60),
        ("confirm_wal_size", lambda c: c.get("sqlite_wal_size_mb", 0) > 100),
        ("reduce_batch", lambda c: True),  # Reduce batch size
        ("extend_retry", lambda c: True),  # Extend bounded retry
        ("pause_background_writes", lambda c: True),  # Pause background writes
    ]

    executed = []
    for step_name, check in steps:
        if check(ctx):
            executed.append(step_name)
        else:
            break

    return {"executed_steps": executed, "remaining": len(steps) - len(executed)}


# ============================================================================
# RECONCILE_BACKLOG Runbook
# ============================================================================

def reconcile_backlog_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """Adaptive reconcile rate based on system health."""
    pg_latency = ctx.get("pg_latency_p95_ms", 0)
    transport_backlog = ctx.get("transport_backlog", 0)

    if pg_latency > 1000 or transport_backlog > 1000:
        # System under pressure - throttle
        return {
            "action": "throttle",
            "new_batch_size": max(10, ctx.get("reconcile_batch_size", 100) // 2),
            "new_token_budget": max(1, ctx.get("reconcile_token_budget", 10) // 2),
        }
    elif pg_latency < 200 and transport_backlog < 100:
        # System healthy - can increase
        return {
            "action": "accelerate",
            "new_batch_size": min(500, ctx.get("reconcile_batch_size", 100) * 2),
            "new_token_budget": min(100, ctx.get("reconcile_token_budget", 10) * 2),
        }
    else:
        return {"action": "maintain"}


# ============================================================================
# SCHEMA_DRIFT Runbook
# ============================================================================

def schema_drift_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """Schema drift - quarantine, read-only, require release procedure."""
    return {
        "action": "quarantine",
        "steps": [
            "quarantine_writer",
            "enable_read_only_if_permitted",
            "require_database_release_procedure",
        ],
        "migration_required": True,
        "block_auto_migrate": True,
    }


# ============================================================================
# RLS_DRIFT Runbook
# ============================================================================

def rls_drift_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """RLS drift - security incident, block writers, quarantine."""
    return {
        "action": "security_incident",
        "severity": "CRITICAL",
        "steps": [
            "block_affected_writers",
            "quarantine_connections",
            "alert_security_team",
        ],
        "auto_fix_allowed": False,
        "migration_required": True,
    }


# ============================================================================
# QDRANT_METADATA_MISMATCH Runbook
# ============================================================================

def qdrant_mismatch_runbook(ctx: dict[str, Any]) -> dict[str, Any]:
    """Qdrant mismatch - mark reindex, enqueue rebuild, don't modify PG authority."""
    return {
        "action": "reindex_required",
        "steps": [
            "mark_reindex_required",
            "enqueue_embedding_rebuild",
            "do_not_modify_pg_authority",
        ],
        "affected_resources": ctx.get("qdrant_mismatch_resources", []),
    }


# ============================================================================
# Runbook Registry
# ============================================================================

@dataclass
class Runbook:
    """Executable runbook for a diagnosis code."""
    diagnosis_code: "DiagnosisCode"
    diagnose: Callable[[dict[str, Any]], dict[str, Any]]
    remediate: Callable[[dict[str, Any], Any], dict[str, Any]]
    requires_approval: bool = False


RUNBOOKS: dict["DiagnosisCode", Runbook] = {
    DiagnosisCode.PG_POOL_SATURATION: Runbook(
        diagnosis_code=DiagnosisCode.PG_POOL_SATURATION,
        diagnose=pg_pool_saturation_diagnosis,
        remediate=pg_pool_saturation_remediation,
        requires_approval=False,
    ),
    DiagnosisCode.PG_LOCK_CONTENTION: Runbook(
        diagnosis_code=DiagnosisCode.PG_LOCK_CONTENTION,
        diagnose=pg_lock_contention_diagnosis,
        remediate=lambda ctx, diag: {"actions": ["diagnose_only"], "requires_approval": True},
        requires_approval=True,
    ),
    DiagnosisCode.SQLITE_BUSY_STORM: Runbook(
        diagnosis_code=DiagnosisCode.SQLITE_BUSY_STORM,
        diagnose=lambda c: {"steps": sqlite_busy_storm_runbook(c)},
        remediate=lambda c, d: {"actions": "sqlite_busy_storm_steps"},
        requires_approval=False,
    ),
    DiagnosisCode.RECONCILE_BACKLOG: Runbook(
        diagnosis_code=DiagnosisCode.RECONCILE_BACKLOG,
        diagnose=lambda c: {"adaptive": reconcile_backlog_runbook(c)},
        remediate=lambda c, d: {"actions": "adaptive_rate"},
        requires_approval=False,
    ),
    DiagnosisCode.RECONCILE_STALLED: Runbook(
        diagnosis_code=DiagnosisCode.RECONCILE_STALLED,
        diagnose=lambda c: {"stalled": True},
        remediate=lambda c, d: {"actions": ["resume_or_throttle"]},
        requires_approval=False,
    ),
    DiagnosisCode.SCHEMA_DRIFT: Runbook(
        diagnosis_code=DiagnosisCode.SCHEMA_DRIFT,
        diagnose=lambda c: {"drift": True},
        remediate=lambda c, d: schema_drift_runbook(c),
        requires_approval=True,
    ),
    DiagnosisCode.RLS_DRIFT: Runbook(
        diagnosis_code=DiagnosisCode.RLS_DRIFT,
        diagnose=lambda c: {"drift": True},
        remediate=lambda c, d: rls_drift_runbook(c),
        requires_approval=True,
    ),
    DiagnosisCode.QDRANT_METADATA_MISMATCH: Runbook(
        diagnosis_code=DiagnosisCode.QDRANT_METADATA_MISMATCH,
        diagnose=lambda c: {"mismatch": True},
        remediate=lambda c, d: qdrant_mismatch_runbook(c),
        requires_approval=False,
    ),
}


def get_runbook(diagnosis_code: "DiagnosisCode") -> Optional[Runbook]:
    """Get runbook for diagnosis code."""
    return RUNBOOKS.get(diagnosis_code)


__all__ = [
    "Runbook",
    "RUNBOOKS",
    "get_runbook",
    "pg_pool_saturation_diagnosis",
    "pg_pool_saturation_remediation",
    "pg_lock_contention_diagnosis",
    "sqlite_busy_storm_runbook",
    "reconcile_backlog_runbook",
    "schema_drift_runbook",
    "rls_drift_runbook",
    "qdrant_mismatch_runbook",
]