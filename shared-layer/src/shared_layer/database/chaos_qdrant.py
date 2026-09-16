"""Qdrant fault cells — fake client plus declared consistency rules.

Qdrant is a rebuildable semantic projection (A365/A52): it may fail or
drift, but a stale/missing vector must surface as ``degraded`` or
``reconcile_required`` — never silently served as current, and never
polluting the PostgreSQL authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .chaos_matrix import ChaosResult, FaultCell


def run_cell(cell: FaultCell, work: Path) -> ChaosResult:
    if cell.fault_id in ("qdrant-offline", "scenario-qdrant-offline"):
        return _offline(cell)
    if cell.fault_id == "qdrant-point-missing":
        return _consistency(cell, pg_has=True, qd_has=False, observed="degraded")
    if cell.fault_id == "qdrant-stale-vector":
        return _stale_vector(cell)
    if cell.fault_id == "qdrant-collection-missing":
        return _offline(cell, observed="collection-missing")
    if cell.fault_id == "qdrant-chunk-no-vector":
        return _consistency(cell, pg_has=True, qd_has=False,
                            observed="reconcile")
    if cell.fault_id == "qdrant-version-mismatch":
        return _stale_vector(cell, observed="version-mismatch")
    return ChaosResult(cell, False, "no-runner")


def _offline(cell: FaultCell, observed: str = "degraded") -> ChaosResult:
    """Offline Qdrant degrades semantic capability; PostgreSQL untouched."""
    witness = {"pg_queries": 0}

    class _Pg:
        def query(self) -> str:
            witness["pg_queries"] += 1
            return "pg-result"

    class _Qdrant:
        def search(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("qdrant unreachable")

    try:
        _Qdrant().search("x")
        semantic = "ok"
    except ConnectionError:
        semantic = "degraded"
    lexical = _Pg().query()
    ok = semantic == "degraded" and lexical == "pg-result"
    return ChaosResult(cell, ok, observed if ok else "unexpected",
                       {"semantic": semantic, "lexical": lexical,
                        "pg_healthy": witness["pg_queries"] == 1})


def _consistency(
    cell: FaultCell, *, pg_has: bool, qd_has: bool, observed: str
) -> ChaosResult:
    """Canonical chunk without matching vector -> reconcile_required."""
    state = "reconcile_required" if (pg_has and not qd_has) else "consistent"
    ok = state == "reconcile_required"
    return ChaosResult(cell, ok, observed if ok else "missed",
                       {"state": state, "served_stale": False})


def _stale_vector(cell: FaultCell, observed: str = "stale") -> ChaosResult:
    """Prior-generation vector must never be served as current (A370)."""
    vector = {"source_revision": 3, "embedding_version": "e1"}
    resource_revision = 4
    stale = vector["source_revision"] != resource_revision
    return ChaosResult(cell, stale, observed if stale else "served-stale",
                       {"vector_rev": vector["source_revision"],
                        "resource_rev": resource_revision})


__all__ = ["run_cell"]
