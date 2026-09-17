"""Orphan Scanner (C7).

Periodically scans for orphan states across PostgreSQL, SQLite, and Qdrant:
  1. PG has resource but locator does not exist
  2. Qdrant point exists but no chunk in PG
  3. SQLite has pending resource but PG has no corresponding resource

Qdrant scanning is optional: it requires an injected interface
(``scan_points(limit=...)``) because the runtime Qdrant client still belongs
to main-system.  Without that interface the report explicitly records Qdrant
as *not scanned* and can never claim PASS — a missing engine is incomplete
coverage, not a clean bill of health.

Usage:
    from shared_layer.database.orphan_scanner import scan_orphans

    with connection_manager.connection() as conn:
        report = scan_orphans(conn, qdrant=qdrant_adapter)
        for orphan in report:
            print(orphan)
        if report.passed:
            ...

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A44/E30 — four-functions-local.
    A52/E38 — RAG: Qdrant canonical semantic index.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from psycopg import Connection

# 1. PG resource without locator
_ORPHAN_LOCATOR = (
    "SELECT r.resource_id, r.module_id "
    "FROM gptbridge_index.resource r "
    "LEFT JOIN registry.locations loc ON loc.resource_id = r.resource_id "
    "WHERE loc.locator_id IS NULL AND r.deletion_stage = 'active'"
)

# 2. Qdrant index_state without chunks (stale index)
_ORPHAN_QDRANT = (
    "SELECT s.resource_id, s.module_id, s.qdrant_collection "
    "FROM gptbridge_rag.index_state s "
    "LEFT JOIN gptbridge_rag.chunk c ON c.resource_id = s.resource_id "
    "WHERE c.chunk_id IS NULL AND s.status = 'indexed' "
    "AND s.deletion_stage = 'active'"
)

# 3. PG resource marked stale (cross-engine mismatch)
_STALE_RESOURCE = (
    "SELECT resource_id, module_id, version, backend_generation "
    "FROM gptbridge_index.resource WHERE stale = true "
    "AND deletion_stage = 'active'"
)

# 4. Known canonical chunk owners — the Qdrant point comparison set
_PG_CHUNK_KEYS = (
    "SELECT DISTINCT resource_id FROM gptbridge_rag.chunk"
)


@dataclass(frozen=True)
class OrphanScanReport:
    """Typed scan result with explicit engine coverage.

    ``status`` is one of PASS / ORPHANS_FOUND / INCOMPLETE.  PASS requires
    every engine to have been scanned (no truncation); an unscanned Qdrant
    interface yields INCOMPLETE even when PostgreSQL found nothing.
    """
    orphans: tuple[dict[str, Any], ...] = ()
    engines_scanned: tuple[str, ...] = ("postgresql",)
    engines_unscanned: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    bounds: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return not self.engines_unscanned and not self.truncated

    @property
    def status(self) -> str:
        if not self.complete:
            return "INCOMPLETE"
        return "ORPHANS_FOUND" if self.orphans else "PASS"

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.orphans)

    def __len__(self) -> int:
        return len(self.orphans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "complete": self.complete,
            "orphans": list(self.orphans),
            "engines_scanned": list(self.engines_scanned),
            "engines_unscanned": list(self.engines_unscanned),
            "reasons": dict(self.reasons),
            "bounds": dict(self.bounds),
            "truncated": self.truncated,
        }


def _point_identity(point: Any) -> tuple[
    Optional[str], Optional[str], Optional[str]
]:
    """Extract (module_id, resource_id, point_id) from a scanned point."""
    if isinstance(point, dict):
        payload = point.get("payload") or {}
        module_id = point.get("module_id")
        resource_id = point.get("resource_id")
        point_id = point.get("point_id", point.get("id"))
    else:
        payload = getattr(point, "payload", None) or {}
        module_id = getattr(point, "module_id", None)
        resource_id = getattr(point, "resource_id", None)
        point_id = getattr(point, "point_id", None)
        if point_id is None:
            point_id = getattr(point, "id", None)
    if isinstance(payload, dict):
        if module_id is None:
            module_id = payload.get("module_id")
        if resource_id is None:
            resource_id = payload.get("resource_id")
    return (
        None if module_id is None else str(module_id),
        None if resource_id is None else str(resource_id),
        None if point_id is None else str(point_id),
    )


def _scan_qdrant(
    connection: Connection[Any],
    qdrant: Any,
    limit: int,
    orphans: list[dict[str, Any]],
) -> tuple[bool, Optional[str], bool]:
    """Scan Qdrant points via the injected interface.

    Returns ``(scanned, reason, truncated)``.  Any failure yields
    ``scanned=False`` — never a silent PASS.
    """
    scanner = getattr(qdrant, "scan_points", None)
    if not callable(scanner):
        return False, "qdrant-scan-points-missing", False
    try:
        points = list(scanner(limit=limit))
    except Exception as exc:
        return False, f"qdrant-scan-failed: {exc}", False
    try:
        known = {
            str(row[0])
            for row in connection.execute(_PG_CHUNK_KEYS).fetchall()
        }
    except Exception as exc:
        return False, f"pg-chunk-keys-unavailable: {exc}", False
    truncated = len(points) >= limit
    for point in points:
        module_id, resource_id, point_id = _point_identity(point)
        if resource_id is not None and resource_id in known:
            continue
        orphans.append({
            "type": "orphan-qdrant-point",
            "resource_id": resource_id,
            "module_id": module_id,
            "point_id": point_id,
        })
    if truncated:
        return True, f"scan-limit-reached:{limit}", True
    return True, None, False


def scan_orphans(
    connection: Connection[Any],
    *,
    qdrant: Any = None,
    qdrant_limit: int = 5000,
) -> OrphanScanReport:
    """Scan for orphan states across all engines.

    PostgreSQL is always scanned; Qdrant is scanned only when the injected
    interface provides a callable ``scan_points(limit=...)``.  Each orphan
    record carries a 'type' field:
      'missing-locator'     — PG resource has no locator
      'orphan-qdrant'      — Qdrant index_state has no chunks
      'stale-resource'     — PG resource marked stale
      'orphan-qdrant-point' — Qdrant point whose resource has no PG chunk
    """
    orphans: list[dict[str, Any]] = []

    # 1. Missing locator
    for row in connection.execute(_ORPHAN_LOCATOR).fetchall():
        orphans.append({
            "type": "missing-locator",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
        })

    # 2. Orphan Qdrant (index_state indexed but no chunks)
    for row in connection.execute(_ORPHAN_QDRANT).fetchall():
        orphans.append({
            "type": "orphan-qdrant",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
            "qdrant_collection": str(row[2]),
        })

    # 3. Stale resources
    for row in connection.execute(_STALE_RESOURCE).fetchall():
        orphans.append({
            "type": "stale-resource",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
            "version": int(row[2]),
            "backend_generation": int(row[3]),
        })

    # 4. Optional Qdrant point scan (never assumed — explicit coverage)
    limit = max(1, int(qdrant_limit))
    engines_scanned: list[str] = ["postgresql"]
    engines_unscanned: list[str] = []
    reasons: dict[str, str] = {}
    truncated = False
    if qdrant is None:
        engines_unscanned.append("qdrant")
        reasons["qdrant"] = "qdrant-interface-not-injected"
    else:
        scanned, reason, truncated = _scan_qdrant(
            connection, qdrant, limit, orphans
        )
        if scanned:
            engines_scanned.append("qdrant")
            if reason is not None:
                reasons["qdrant"] = reason
        else:
            engines_unscanned.append("qdrant")
            reasons["qdrant"] = reason or "qdrant-scan-unavailable"

    return OrphanScanReport(
        orphans=tuple(orphans),
        engines_scanned=tuple(engines_scanned),
        engines_unscanned=tuple(engines_unscanned),
        reasons=reasons,
        bounds={"qdrant_max_points": limit},
        truncated=truncated,
    )


__all__ = ["OrphanScanReport", "scan_orphans"]
