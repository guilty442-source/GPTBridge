"""Executable chaos harness — public entry point (A365-A370, A499).

Dispatches each declared ``FaultCell`` to its per-engine runner
(``chaos_pg`` / ``chaos_sqlite`` / ``chaos_qdrant``).  Controlled test
scope only (A370 FORBID: fault injection against uncontrolled production
scope): every cell runs against temporary files or scripted doubles.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .chaos_matrix import CHAOS_MATRIX, ChaosReport, ChaosResult, FaultCell


def execute_cell(cell: FaultCell, work_dir: Path) -> ChaosResult:
    """Run one cell under the controlled-scope fault injector."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if cell.engine == "postgresql":
        from . import chaos_pg

        return chaos_pg.run_cell(cell, work)
    if cell.engine == "sqlite":
        from . import chaos_sqlite

        return chaos_sqlite.run_cell(cell, work)
    if cell.engine == "qdrant":
        from . import chaos_qdrant

        return chaos_qdrant.run_cell(cell, work)
    return ChaosResult(cell, False, "unknown-engine")


def execute_matrix(
    work_dir: Path,
    *,
    engines: Optional[tuple[str, ...]] = None,
) -> ChaosReport:
    """Execute the declared matrix; each cell under ``work_dir/<fault_id>``."""
    cells = (
        CHAOS_MATRIX if engines is None
        else tuple(c for c in CHAOS_MATRIX if c.engine in engines)
    )
    report = ChaosReport(total=len(cells))
    for cell in cells:
        cell_dir = Path(work_dir) / cell.fault_id
        try:
            result = execute_cell(cell, cell_dir)
        except Exception as exc:
            result = ChaosResult(cell, False, f"runner-error: {exc}")
        report.results.append(result)
        if result.passed:
            report.passed += 1
        else:
            report.failed += 1
    return report


__all__ = ["execute_cell", "execute_matrix"]
