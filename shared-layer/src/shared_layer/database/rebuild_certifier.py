"""Rebuild Certifier (migration 028 + D2).

After Qdrant rebuild, PostgreSQL restore, or SQLite repair, the engine
cannot go live until it passes count/hash/revision/RLS/locator checks —
not just SELECT 1.  This module runs those checks and records the result.

Usage:
    from shared_layer.database.rebuild_certifier import certify

    result = certify(
        connection,
        engine="qdrant",
        target="gptbridge_shared_knowledge",
        rebuild_reason="rebuild",
        checks=[...],
        certified_by="rebuild-job",
    )

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A52/E38 — RAG: Qdrant canonical semantic index.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional
from uuid import UUID

from psycopg import Connection

_RECORD = (
    "SELECT gptbridge_index.record_rebuild_certification("
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_IS_CERTIFIED = "SELECT gptbridge_index.is_engine_certified(%s, %s)"


def certify(
    connection: Connection[Any],
    *,
    engine: str,
    target: str,
    rebuild_reason: str,
    checks: list[dict[str, Any]],
    certified_by: str,
    resource_count: Optional[int] = None,
    content_hash: Optional[str] = None,
    schema_version: Optional[str] = None,
    rls_verified: bool = False,
    locator_verified: bool = False,
) -> Optional[UUID]:
    """Record a rebuild certification result.

    The caller runs the checks (count, hash, revision, RLS, locator) and
    passes the results.  The function records them and returns the
    certification_id.  Certification passes only if all checks passed.
    """
    import json

    passed = sum(1 for c in checks if c.get("passed", False))
    total = len(checks)
    row = connection.execute(
        _RECORD,
        (
            engine, target, rebuild_reason, json.dumps(checks),
            passed, total, resource_count, content_hash, schema_version,
            rls_verified, locator_verified, certified_by,
        ),
    ).fetchone()
    if row and row[0]:
        return UUID(str(row[0]))
    return None


def is_certified(
    connection: Connection[Any],
    *,
    engine: str,
    target: str,
) -> bool:
    """Check if an engine's latest rebuild is certified."""
    row = connection.execute(_IS_CERTIFIED, (engine, target)).fetchone()
    return bool(row and row[0])


@dataclass
class EngineCertificationResult:
    """Typed outcome of the production rebuild-certification entry."""

    certified: bool
    certification_id: Optional[UUID] = None
    checks: list[dict[str, Any]] | None = None
    engine_live: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "certified": self.certified,
            "certification_id": (
                str(self.certification_id) if self.certification_id else None
            ),
            "engine_live": self.engine_live,
            "detail": self.detail,
            "checks": list(self.checks or []),
        }


def _probe(connection: Any) -> tuple[bool, str]:
    if connection is None:
        return False, "no-connection"
    try:
        row = connection.execute("SELECT 1").fetchone()
    except Exception as exc:
        return False, f"probe-failed:{exc}"[:200]
    return bool(row and int(row[0]) == 1), "SELECT 1"


def run_check_runners(
    connection: Any,
    runners: list[Callable[[Any], Any]],
) -> list[dict[str, Any]]:
    """Run injected check callables; a raising runner is a failed check."""
    checks: list[dict[str, Any]] = []
    for runner in runners:
        name = getattr(runner, "__name__", None) or "check"
        try:
            outcome = runner(connection)
        except Exception as exc:
            checks.append({
                "name": name,
                "passed": False,
                "detail": f"error:{exc}"[:200],
            })
            continue
        if isinstance(outcome, dict):
            entry = dict(outcome)
            entry.setdefault("name", name)
            checks.append(entry)
        elif isinstance(outcome, tuple) and len(outcome) == 2:
            checks.append({
                "name": name,
                "passed": bool(outcome[0]),
                "detail": str(outcome[1])[:200],
            })
        else:
            checks.append({
                "name": name,
                "passed": bool(outcome),
                "detail": "",
            })
    return checks


def certify_engine(
    connection: Any,
    *,
    engine: str,
    target: str,
    rebuild_reason: str,
    certified_by: str,
    checks: Optional[list[dict[str, Any]]] = None,
    check_runners: Optional[list[Callable[[Any], Any]]] = None,
    resource_count: Optional[int] = None,
    content_hash: Optional[str] = None,
    schema_version: Optional[str] = None,
    rls_verified: bool = False,
    locator_verified: bool = False,
) -> EngineCertificationResult:
    """Production entry: run/inject checks, record, and verify the record.

    ``check_runners`` is the injection point for engine-specific checks
    (count, hash, revision, RLS, locator).  ``certified=True`` requires a
    live connection, at least one passed check and a read-back confirmation
    from ``is_engine_certified`` — evidence is only ever recorded, never
    inferred from a missing check.
    """
    live, live_detail = _probe(connection)
    evidence = list(checks or [])
    if check_runners:
        evidence += run_check_runners(connection, check_runners)
    if not live:
        return EngineCertificationResult(
            certified=False,
            certification_id=None,
            checks=evidence,
            engine_live=False,
            detail=f"engine-not-live:{live_detail}",
        )
    if not evidence:
        return EngineCertificationResult(
            certified=False,
            certification_id=None,
            checks=[],
            engine_live=True,
            detail="no-checks-provided",
        )
    all_passed = all(c.get("passed") is True for c in evidence)
    certification_id = certify(
        connection,
        engine=engine,
        target=target,
        rebuild_reason=rebuild_reason,
        checks=evidence,
        certified_by=certified_by,
        resource_count=resource_count,
        content_hash=content_hash,
        schema_version=schema_version,
        rls_verified=rls_verified,
        locator_verified=locator_verified,
    )
    certified = False
    detail = "" if all_passed else "checks-not-passed"
    if all_passed and certification_id is not None:
        try:
            certified = bool(
                is_certified(connection, engine=engine, target=target)
            )
            if not certified:
                detail = "round-trip-not-certified"
        except Exception as exc:
            certified = False
            detail = f"round-trip-failed:{exc}"[:200]
    return EngineCertificationResult(
        certified=certified,
        certification_id=certification_id,
        checks=evidence,
        engine_live=True,
        detail=detail,
    )


__all__ = [
    "EngineCertificationResult",
    "certify",
    "certify_engine",
    "is_certified",
    "run_check_runners",
]
