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

from typing import Any, Optional
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


__all__ = ["certify", "is_certified"]
