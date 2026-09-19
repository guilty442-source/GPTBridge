"""Binding Dependency Order — A367 Implementation.

A367: The binding dependency order is exactly:
1. RECONCILIATION (PostgreSQL<->SQLite authority/revision/hash/status)
2. GLOBAL_ID_VERSION (one resource_id and version across all modules)

This module enforces the mandatory execution order for binding operations.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .reconciliation import ReconcileService, ReconcileResult
from .global_id_version import GlobalIdVersionService

_logger = logging.getLogger("gptbridge.binding.order")


class BindingPhase(Enum):
    """Binding phases in mandatory order (A367)."""
    RECONCILIATION = 1
    GLOBAL_ID_VERSION = 2


@dataclass(frozen=True)
class BindingOrderResult:
    """Result of executing binding operations in order."""
    phase: BindingPhase
    success: bool
    results: list[Any] = field(default_factory=list)
    error: Optional[str] = None


class BindingOrderEnforcer:
    """Enforces A367 binding dependency order: RECONCILIATION -> GLOBAL_ID_VERSION."""

    def __init__(
        self,
        reconciler: ReconcileService,
        global_id_service: GlobalIdVersionService,
    ) -> None:
        self.reconciler = reconciler
        self.global_id_service = global_id_service
        self._completed_phases: set[BindingPhase] = set()
        self._lock = asyncio.Lock()

    async def execute_in_order(
        self,
        module_id: str,
        *,
        batch_size: int = 100,
    ) -> list[BindingOrderResult]:
        """Execute binding operations in mandatory A367 order.

        Phase 1: RECONCILIATION - PostgreSQL<->SQLite authority/revision/hash/status
        Phase 2: GLOBAL_ID_VERSION - one resource_id and version across all modules

        Returns results for each phase.
        """
        results: list[BindingOrderResult] = []

        async with self._lock:
            # Phase 1: RECONCILIATION
            results.append(await self._execute_reconciliation_phase(module_id, batch_size))
            if not results[-1].success:
                return results  # Stop on failure

            # Phase 2: GLOBAL_ID_VERSION
            results.append(await self._execute_global_id_phase(module_id, results[-1].results))

        return results

    async def _execute_reconciliation_phase(
        self,
        module_id: str,
        batch_size: int,
    ) -> BindingOrderResult:
        """Execute Phase 1: RECONCILIATION."""
        _logger.info("BindingOrderEnforcer: Phase 1 - RECONCILIATION for %s", module_id)
        reconciliation_results = []
        try:
            async for result in self.reconciler.reconcile_module(module_id, batch_size=batch_size):
                reconciliation_results.append(result)
            self._completed_phases.add(BindingPhase.RECONCILIATION)
            _logger.info("BindingOrderEnforcer: RECONCILIATION completed for %s (%d results)",
                       module_id, len(reconciliation_results))
            return BindingOrderResult(
                phase=BindingPhase.RECONCILIATION,
                success=True,
                results=reconciliation_results,
            )
        except Exception as exc:
            _logger.error("BindingOrderEnforcer: RECONCILIATION failed for %s: %s", module_id, exc)
            return BindingOrderResult(
                phase=BindingPhase.RECONCILIATION,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _execute_global_id_phase(
        self,
        module_id: str,
        reconciliation_results: list[Any],
    ) -> BindingOrderResult:
        """Execute Phase 2: GLOBAL_ID_VERSION."""
        _logger.info("BindingOrderEnforcer: Phase 2 - GLOBAL_ID_VERSION for %s", module_id)
        global_id_results = []
        try:
            reconciled_resources = self._filter_reconciled_resources(reconciliation_results)

            for resource_id in reconciled_resources:
                gid_result = await self.global_id_service.assign_global_id(
                    module_id, resource_id
                )
                global_id_results.append(gid_result)

            self._completed_phases.add(BindingPhase.GLOBAL_ID_VERSION)
            _logger.info("BindingOrderEnforcer: GLOBAL_ID_VERSION completed for %s (%d resources)",
                       module_id, len(global_id_results))
            return BindingOrderResult(
                phase=BindingPhase.GLOBAL_ID_VERSION,
                success=True,
                results=global_id_results,
            )
        except Exception as exc:
            _logger.error("BindingOrderEnforcer: GLOBAL_ID_VERSION failed for %s: %s", module_id, exc)
            return BindingOrderResult(
                phase=BindingPhase.GLOBAL_ID_VERSION,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _filter_reconciled_resources(
        self,
        reconciliation_results: list[ReconcileResult],
    ) -> list[str]:
        """Filter resources that were successfully reconciled."""
        return [
            r.resource_id for r in reconciliation_results
            if r.action in ("pushed", "pulled", "in-sync")
        ]

    def get_completed_phases(self) -> set[BindingPhase]:
        """Get phases that have been successfully completed."""
        return set(self._completed_phases)

    def reset(self) -> None:
        """Reset completed phases (for testing/retry)."""
        self._completed_phases.clear()

    def is_phase_completed(self, phase: BindingPhase) -> bool:
        return phase in self._completed_phases


# Convenience function for creating enforcer with default services
async def create_binding_order_enforcer(
    sqlite_connection: Any,
    pg_connection: Any,
) -> BindingOrderEnforcer:
    """Create a BindingOrderEnforcer with default service instances."""
    from .reconciliation import ReconcileService
    from .global_id_version import GlobalIdVersionService

    reconciler = ReconcileService(sqlite_connection, pg_connection)
    global_id_service = GlobalIdVersionService(pg_connection)
    return BindingOrderEnforcer(reconciler, global_id_service)


__all__ = [
    "BindingPhase",
    "BindingOrderResult",
    "BindingOrderEnforcer",
    "create_binding_order_enforcer",
]