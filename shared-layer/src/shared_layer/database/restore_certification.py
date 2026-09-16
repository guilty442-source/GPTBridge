"""Restore Certification (A44/E30 + A8/E21 + A46/E22).

After a restore completes, the database must pass a certification gate before
it is marked healthy.  The gate verifies:
  1. Schema matches the declared contract
  2. RLS is enabled and forced on all protected tables
  3. Resource count is within expected bounds
  4. Audit head matches the pre-restore head (or is documented)
  5. Qdrant linkage is intact (index_state rows have valid qdrant_point_id)
  6. Reconcile state is clean (no pending rows)
  7. Generation fence is bumped
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema_contract_registry import verify_contract, ContractVerificationResult


@dataclass
class CertificationCheck:
    """One certification check result."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class RestoreCertificationResult:
    """Full restore certification result."""

    passed: bool
    checks: list[CertificationCheck] = field(default_factory=list)
    generation_before: int = 0
    generation_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


def certify_restore(
    connection: Any,
    *,
    pre_restore_audit_head: str | None = None,
    pre_restore_generation: int = 1,
    max_resource_count: int = 10_000_000,
    bump_generation: bool = True,
) -> RestoreCertificationResult:
    """Certify a restored PostgreSQL database.

    This function is read-only except for the optional generation bump.
    """
    checks: list[CertificationCheck] = []

    # 1. Schema contract verification
    contract_result = verify_contract(connection)
    checks.append(CertificationCheck(
        name="schema_contract",
        passed=contract_result.passed,
        detail=f"{len(contract_result.drifts)} drifts" if contract_result.drifts else "ok",
    ))

    # 2. RLS verification (covered by schema contract, but check explicitly)
    try:
        rls_failures: list[str] = []
        rows = connection.execute(
            """
            SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname LIKE 'gptbridge_%'
              AND c.relkind = 'r'
            """
        ).fetchall()
        for row in rows:
            schema, table, rls_on, rls_forced = row
            if not rls_on:
                rls_failures.append(f"{schema}.{table}: rls_not_enabled")
            if not rls_forced:
                rls_failures.append(f"{schema}.{table}: rls_not_forced")
        checks.append(CertificationCheck(
            name="rls_enabled",
            passed=len(rls_failures) == 0,
            detail="; ".join(rls_failures) if rls_failures else "all tables protected",
        ))
    except Exception as exc:
        checks.append(CertificationCheck("rls_enabled", False, str(exc)[:200]))

    # 3. Resource count within bounds
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_index.resource"
        ).fetchone()
        count = int(row[0]) if row else 0
        checks.append(CertificationCheck(
            name="resource_count",
            passed=count <= max_resource_count,
            detail=f"count={count} max={max_resource_count}",
        ))
    except Exception as exc:
        checks.append(CertificationCheck("resource_count", False, str(exc)[:200]))

    # 4. Audit head matches pre-restore
    try:
        row = connection.execute(
            "SELECT event_id::text FROM gptbridge_audit.event ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
        current_head = str(row[0]) if row else ""
        if pre_restore_audit_head is not None:
            head_matches = current_head == pre_restore_audit_head
            checks.append(CertificationCheck(
                name="audit_head",
                passed=head_matches,
                detail=f"current={current_head[:8]} expected={pre_restore_audit_head[:8]}",
            ))
        else:
            checks.append(CertificationCheck(
                name="audit_head",
                passed=True,
                detail=f"head={current_head[:8]} (no pre-restore head provided)",
            ))
    except Exception as exc:
        checks.append(CertificationCheck("audit_head", False, str(exc)[:200]))

    # 5. Qdrant linkage intact
    try:
        row = connection.execute(
            """
            SELECT count(*) FROM gptbridge_rag.index_state
            WHERE qdrant_point_id IS NULL OR qdrant_point_id = ''
            """
        ).fetchone()
        missing_qdrant = int(row[0]) if row else 0
        checks.append(CertificationCheck(
            name="qdrant_linkage",
            passed=missing_qdrant == 0,
            detail=f"missing_qdrant_linkage={missing_qdrant}",
        ))
    except Exception as exc:
        checks.append(CertificationCheck("qdrant_linkage", False, str(exc)[:200]))

    # 6. Reconcile state clean
    try:
        # Check SQLite reconcile_state — this is a heuristic since we can't
        # access SQLite from here.  We check the PostgreSQL conflict log.
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_index.reconcile_conflict_log WHERE resolution_action = 'pending'"
        ).fetchone()
        pending_conflicts = int(row[0]) if row else 0
        checks.append(CertificationCheck(
            name="reconcile_state",
            passed=pending_conflicts == 0,
            detail=f"pending_conflicts={pending_conflicts}",
        ))
    except Exception as exc:
        checks.append(CertificationCheck("reconcile_state", False, str(exc)[:200]))

    # 7. Generation fence bump
    generation_after = pre_restore_generation
    if bump_generation:
        try:
            row = connection.execute(
                "SELECT gptbridge_index.bump_backend_generation(%s, %s)",
                ("restore_certification", "restore_certify"),
            ).fetchone()
            generation_after = int(row[0]) if row else pre_restore_generation + 1
            checks.append(CertificationCheck(
                name="generation_fence",
                passed=True,
                detail=f"bumped {pre_restore_generation} -> {generation_after}",
            ))
        except Exception as exc:
            checks.append(CertificationCheck("generation_fence", False, str(exc)[:200]))
    else:
        try:
            row = connection.execute(
                "SELECT gptbridge_index.current_backend_generation()"
            ).fetchone()
            generation_after = int(row[0]) if row else pre_restore_generation
            checks.append(CertificationCheck(
                name="generation_fence",
                passed=True,
                detail=f"current={generation_after} (no bump)",
            ))
        except Exception as exc:
            checks.append(CertificationCheck("generation_fence", False, str(exc)[:200]))

    all_passed = all(c.passed for c in checks)
    return RestoreCertificationResult(
        passed=all_passed,
        checks=checks,
        generation_before=pre_restore_generation,
        generation_after=generation_after,
    )


__all__ = [
    "CertificationCheck",
    "RestoreCertificationResult",
    "certify_restore",
]
