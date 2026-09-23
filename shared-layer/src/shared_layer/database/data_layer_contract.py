"""Data Layer Contract (Phase K).

Runtime helpers for the data layer master contract:
- Contract registration and activation
- Dependency classification (authority/required/degradable/optional)
- Startup phase sequence (PHASE 0-8)
- Startup phase gates
- Per-schema readiness
- RAG readiness gate
- Shutdown phase sequence
- Shutdown audit
- Unclean shutdown detection
- Cache invalidation policy
- Dependency graph
- Integration rules (12 formal rules)

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist.
    A44/E30 — four-functions-local.
    A46/E22 — Audit: mandatory-ledger.
    A52/E38 — RAG: Qdrant canonical semantic index.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Final

# Governance codex authority (A107/A173): PostgreSQL ``gptbridge_codex``
# reached only through the governed repository interface.
_DEFAULT_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[4]


def register_data_layer_contract(
    conn: Any, contract_version: int, **kwargs: Any,
) -> str:
    """Register a new data layer contract version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_data_layer_contract(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (contract_version,
             kwargs.get("database_release_id"),
             kwargs.get("postgresql_schema_version"),
             kwargs.get("sqlite_template_version"),
             kwargs.get("qdrant_contract_version"),
             kwargs.get("security_generation"),
             kwargs.get("data_generation"),
             kwargs.get("required_capabilities", []),
             kwargs.get("optional_capabilities", []),
             kwargs.get("startup_order", []),
             kwargs.get("shutdown_order", []),
             kwargs.get("degradation_policy"),
             kwargs.get("recovery_policy")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def activate_data_layer_contract(conn: Any, contract_id: str) -> None:
    """Activate a data layer contract (supersedes previous active)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.activate_data_layer_contract(%s)",
            (contract_id,),
        )
    conn.commit()


def get_active_data_layer_contract(conn: Any) -> dict | None:
    """Get the currently active data layer contract."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gptbridge_index.get_active_data_layer_contract()")
        row = cur.fetchone()
    if not row:
        return None
    return {
        "contract_id": row[0], "contract_version": row[1],
        "database_release_id": row[2], "postgresql_schema_version": row[3],
        "sqlite_template_version": row[4], "qdrant_contract_version": row[5],
        "security_generation": row[6], "data_generation": row[7],
        "required_capabilities": row[8], "optional_capabilities": row[9],
        "startup_order": row[10], "shutdown_order": row[11],
    }


def classify_dependency(
    conn: Any, component_name: str, dependency_type: str,
    component_category: str, failure_effect: str, **kwargs: Any,
) -> str:
    """Classify a data component's dependency type."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.classify_dependency(%s, %s, %s, %s, %s, %s, %s, %s)",
            (component_name, dependency_type, component_category,
             failure_effect, kwargs.get("criticality", "capability-critical"),
             kwargs.get("fallback_component"),
             kwargs.get("fallback_boundary"),
             kwargs.get("description")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def get_dependency_classification(
    conn: Any, component_name: str,
) -> tuple | None:
    """Get dependency classification for a component."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM gptbridge_index.get_dependency_classification(%s)",
            (component_name,),
        )
        row = cur.fetchone()
    return tuple(row) if row else None


def get_startup_order(conn: Any) -> list[tuple]:
    """Get the startup phase order."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gptbridge_index.get_startup_order()")
        rows = cur.fetchall()
    return [tuple(r) for r in rows]


def get_shutdown_order(conn: Any) -> list[tuple]:
    """Get the shutdown phase order."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gptbridge_index.get_shutdown_order()")
        rows = cur.fetchall()
    return [tuple(r) for r in rows]


def register_startup_gate(
    conn: Any, phase_number: int, gate_name: str, gate_type: str,
    **kwargs: Any,
) -> str:
    """Register a startup phase gate."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_startup_gate(%s, %s, %s, %s, %s, %s)",
            (phase_number, gate_name, gate_type,
             kwargs.get("check_expression"),
             kwargs.get("required_for_write", False),
             kwargs.get("required_for_requests", False)),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def set_gate_result(
    conn: Any, gate_id: str, passed: bool,
    failure_reason: str | None = None,
) -> None:
    """Set the result of a startup gate check."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.set_gate_result(%s, %s, %s)",
            (gate_id, passed, failure_reason),
        )
    conn.commit()


def is_phase_complete(conn: Any, phase_number: int) -> bool:
    """Check if all gates for a phase have passed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_phase_complete(%s)",
            (phase_number,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def can_enable_write(conn: Any, phase_number: int) -> bool:
    """Check if write can be enabled for a phase."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.can_enable_write(%s)",
            (phase_number,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def set_schema_readiness(
    conn: Any, schema_name: str, ready: bool, **kwargs: Any,
) -> None:
    """Set readiness status for a PostgreSQL schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.set_schema_readiness(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (schema_name, ready,
             kwargs.get("schema_version"),
             kwargs.get("migration_head"),
             kwargs.get("rls_enabled"),
             kwargs.get("force_rls"),
             kwargs.get("required_roles"),
             kwargs.get("public_grants_revoked"),
             kwargs.get("security_generation"),
             kwargs.get("failure_reason")),
        )
    conn.commit()


def is_schema_ready(conn: Any, schema_name: str) -> bool:
    """Check if a specific schema is ready."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_schema_ready(%s)",
            (schema_name,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def is_pg_certified(conn: Any) -> bool:
    """Check if PostgreSQL is fully certified (all required schemas ready)."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.is_pg_certified()")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def can_enable_business_write(conn: Any) -> bool:
    """Check if business writes can be enabled.
    Requires: authority_ready AND audit_ready AND security_ready.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.can_enable_business_write()")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def evaluate_rag_readiness(conn: Any) -> bool:
    """Evaluate RAG readiness (PG metadata + Qdrant + authority + contract)."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.evaluate_rag_readiness()")
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


def is_rag_ready(conn: Any) -> bool:
    """Check if RAG is ready."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.is_rag_ready()")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def start_shutdown_audit(conn: Any) -> str:
    """Start a shutdown audit record."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.start_shutdown_audit()")
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def complete_shutdown_audit(
    conn: Any, shutdown_id: str, status: str, **kwargs: Any,
) -> None:
    """Complete a shutdown audit record."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.complete_shutdown_audit(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (shutdown_id, status,
             kwargs.get("drain_result"),
             kwargs.get("pending_ops", 0),
             kwargs.get("pending_transport", 0),
             kwargs.get("reconcile_pending", 0),
             kwargs.get("leases_released", 0),
             kwargs.get("leases_expired", 0),
             kwargs.get("sqlite_checkpoints", 0),
             kwargs.get("qdrant_cursor_saved", False),
             kwargs.get("audit_flushed", False),
             kwargs.get("notes")),
        )
    conn.commit()


def was_last_shutdown_graceful(conn: Any) -> bool:
    """Check if the last shutdown was graceful."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.was_last_shutdown_graceful()")
        row = cur.fetchone()
    return bool(row[0]) if row else False


def detect_unclean_shutdown(conn: Any) -> tuple[bool, list[str]]:
    """Detect if last shutdown was unclean and get extra recovery steps."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gptbridge_index.detect_unclean_shutdown()")
        row = cur.fetchone()
    conn.commit()
    if not row:
        return True, []
    return bool(row[0]), list(row[1]) if row[1] else []


def mark_unclean_step_done(
    conn: Any, detection_id: str, step_name: str,
) -> None:
    """Mark an unclean shutdown recovery step as done."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.mark_unclean_step_done(%s, %s)",
            (detection_id, step_name),
        )
    conn.commit()


def is_unclean_recovery_complete(conn: Any) -> bool:
    """Check if all unclean shutdown recovery steps are done."""
    with conn.cursor() as cur:
        cur.execute("SELECT gptbridge_index.is_unclean_recovery_complete()")
        row = cur.fetchone()
    return bool(row[0]) if row else True


def register_cache_policy(
    conn: Any, cache_name: str, **kwargs: Any,
) -> str:
    """Register a cache invalidation policy."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_cache_policy(%s, %s, %s, %s, %s, %s)",
            (cache_name,
             kwargs.get("ttl_seconds"),
             kwargs.get("on_mismatch", "invalidate"),
             kwargs.get("check_gen", True),
             kwargs.get("check_rev", True),
             kwargs.get("check_ttl", True)),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def should_invalidate_cache(
    conn: Any, cache_name: str,
    generation_compatible: bool, revision_compatible: bool,
    ttl_valid: bool,
) -> bool:
    """Check if a cache should be invalidated."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.should_invalidate_cache(%s, %s, %s, %s)",
            (cache_name, generation_compatible, revision_compatible, ttl_valid),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else True


def get_dependencies(conn: Any, component: str) -> list[tuple]:
    """Get dependencies of a component from the dependency graph."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM gptbridge_index.get_dependencies(%s)",
            (component,),
        )
        rows = cur.fetchall()
    return [tuple(r) for r in rows]


def get_dependents(conn: Any, component: str) -> list[tuple]:
    """Get dependents of a component from the dependency graph."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM gptbridge_index.get_dependents(%s)",
            (component,),
        )
        rows = cur.fetchall()
    return [tuple(r) for r in rows]


def get_integration_rules(conn: Any) -> list[tuple]:
    """Get all active integration rules."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gptbridge_index.get_integration_rules()")
        rows = cur.fetchall()
    return [tuple(r) for r in rows]


def check_integration_rule(
    conn: Any, rule_number: int,
) -> tuple | None:
    """Check a specific integration rule."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM gptbridge_index.check_integration_rule(%s)",
            (rule_number,),
        )
        row = cur.fetchone()
    return tuple(row) if row else None


def get_capability_degradation_matrix(
    codex_db_path: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the codex-declared SQL capability degradation matrix.

    The matrix is declared exactly once in the governance codex
    (``sql_capability_degradation_matrix``); this is a read-only runtime
    projection so degradation handling consults the single authority
    instead of declaring a second copy.  An absent or unreadable codex
    returns an empty mapping — callers fail closed on missing rows.
    """
    try:
        if codex_db_path is not None:
            # Explicit path = predecessor/staging fixture, never authority.
            database = Path(codex_db_path)
            if not database.is_file():
                return {}
            connection_ctx = sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True
            )
        else:
            from governance_rule.execution.codex_repository import (
                codex_readonly_connection,
            )

            connection_ctx = codex_readonly_connection()
        with connection_ctx as connection:
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(sql_capability_degradation_matrix)"
                )
            ]
            if not columns:
                return {}
            rows = connection.execute(
                "SELECT "
                + ", ".join(columns)
                + " FROM sql_capability_degradation_matrix"
            ).fetchall()
    except Exception:
        return {}
    matrix: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(zip(columns, row))
        capability = str(record.get("capability_code") or "").strip()
        if capability:
            matrix[capability] = record
    return matrix


__all__ = [
    "register_data_layer_contract",
    "activate_data_layer_contract",
    "get_active_data_layer_contract",
    "classify_dependency",
    "get_dependency_classification",
    "get_startup_order",
    "get_shutdown_order",
    "register_startup_gate",
    "set_gate_result",
    "is_phase_complete",
    "can_enable_write",
    "set_schema_readiness",
    "is_schema_ready",
    "is_pg_certified",
    "can_enable_business_write",
    "evaluate_rag_readiness",
    "is_rag_ready",
    "start_shutdown_audit",
    "complete_shutdown_audit",
    "was_last_shutdown_graceful",
    "detect_unclean_shutdown",
    "mark_unclean_step_done",
    "is_unclean_recovery_complete",
    "register_cache_policy",
    "should_invalidate_cache",
    "get_dependencies",
    "get_dependents",
    "get_integration_rules",
    "check_integration_rule",
    "get_capability_degradation_matrix",
]
