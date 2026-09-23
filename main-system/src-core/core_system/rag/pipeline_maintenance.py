"""Canonical RAG background maintenance — bounded, restart-safe, fail-closed.

One governed pass over the canonical data path:

    1. ``reset_stale_outbox_leases`` — reclaim PROCESSING rows orphaned
       by a crashed worker so unfinished outbox work resumes (RAG-08).
    2. ``process_outbox`` — drain pending / due-retry physical ops.
    3. ``purge_tombstones`` — flip aged tombstones to ``purged`` only
       after the physical vector state is verified gone; live residue is
       re-enqueued as durable DELETE_RESOURCE work instead (RAG-10).
    4. optional ``run_parity_sweep`` on a slower cadence — incremental
       canonical inspection + automatic Qdrant repair through the
       durable reconciliation queue (§10.6).

Every step is bounded by batch limits and a wall-clock budget.  Nothing
here ever deletes the Qdrant collection, rebuilds undrifted resources,
or bypasses the outbox for physical mutations.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from .pipeline_outbox import _OUTBOX_MAX_ATTEMPTS
from .rag_contracts import OutboxOperation
from .runtime_state import RagRuntimeState

_logger = logging.getLogger("gptbridge.rag")


class PipelineMaintenanceMixin:
    """Bounded canonical maintenance cycle (lease recovery / outbox drain /
    tombstone purge / parity repair)."""

    # Lease horizon must exceed the slowest legitimate apply attempt
    # (Qdrant client timeout is 30 s) so a live worker's lease is never
    # reclaimed mid-flight.
    _OUTBOX_STALE_LEASE_S = int(
        os.environ.get("RAG_OUTBOX_STALE_LEASE_S", "300")
    )
    _MAINTENANCE_BUDGET_S = float(
        os.environ.get("RAG_MAINTENANCE_BUDGET_S", "30")
    )
    # Tombstones must outlive their retention horizon before physical
    # purge is even considered — purge lifts the read barrier.
    _TOMBSTONE_PURGE_AFTER_S = int(
        os.environ.get("RAG_TOMBSTONE_PURGE_AFTER_S", "3600")
    )
    _TOMBSTONE_PURGE_BATCH = int(
        os.environ.get("RAG_TOMBSTONE_PURGE_BATCH", "50")
    )

    async def run_maintenance_cycle(
        self,
        *,
        budget_seconds: Optional[float] = None,
        include_parity: bool = False,
    ) -> dict[str, Any]:
        """One bounded maintenance pass over the canonical RAG stores.

        Fail-closed: outside CANONICAL the cycle is a no-op (recovery owns
        repair while DEGRADED); every individual step degrades to a counted
        no-op when the authority is unavailable.  ``include_parity`` adds
        the incremental index_state↔Qdrant sweep with drain — callers keep
        it on a slower cadence than the outbox pass.
        """
        started = time.monotonic()
        budget = float(
            budget_seconds
            if budget_seconds is not None
            else self._MAINTENANCE_BUDGET_S
        )
        deadline = started + max(budget, 0.0)
        report: dict[str, Any] = {
            "budget_seconds": budget,
            "duration_ms": 0,
            "budget_exceeded": False,
        }
        state = self._state_machine.state
        if state != RagRuntimeState.CANONICAL:
            report["skipped"] = f"state={state.value}"
            report["duration_ms"] = int((time.monotonic() - started) * 1000)
            return report

        # 1. Crash recovery — orphaned PROCESSING leases back to RETRY
        #    (or DEAD_LETTER once attempts are exhausted).
        reset = getattr(self.postgresql, "reset_stale_outbox_leases", None)
        if reset is not None:
            try:
                report["lease_recovery"] = await reset(
                    stale_after_seconds=self._OUTBOX_STALE_LEASE_S,
                    max_attempts=_OUTBOX_MAX_ATTEMPTS,
                )
            except Exception as exc:  # noqa: BLE001 — maintenance must not raise
                report["lease_recovery"] = {"error": str(exc)}

        # 2. Resume unfinished work — pending + due-retry + reclaimed leases.
        if time.monotonic() < deadline:
            try:
                report["outbox"] = await self.process_outbox()
            except Exception as exc:  # noqa: BLE001
                report["outbox"] = {"error": str(exc)}

        # 3. Tombstone closure — physical purge only after verified-empty
        #    vector state; residue is repaired, never purged.
        if time.monotonic() < deadline:
            report["tombstones"] = await self.purge_tombstones()

        # 4. Optional incremental parity sweep + repair drain (slow cadence).
        if include_parity and time.monotonic() < deadline:
            try:
                report["parity"] = await self.run_parity_sweep(drain=True)
            except Exception as exc:  # noqa: BLE001
                report["parity"] = {"error": str(exc)}

        elapsed = time.monotonic() - started
        report["duration_ms"] = int(elapsed * 1000)
        report["budget_exceeded"] = time.monotonic() > deadline
        if report["budget_exceeded"]:
            _logger.warning(
                "PipelineMaintenance: cycle exceeded budget %.1fs", budget
            )
        return report

    async def purge_tombstones(
        self,
        *,
        older_than_seconds: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, int]:
        """Tombstone cleanup closure (RAG-10).

        For each aged, unpurged tombstone:
          * Qdrant point count unverifiable → keep tombstone (fail-closed).
          * Residual points > 0 → enqueue a durable DELETE_RESOURCE outbox
            event (orphan repair); the tombstone stays until the physical
            delete is verified — never purge over live residue.
          * Zero points → purge canonical metadata (chunks + index_state)
            and mark the tombstone ``purged``.

        Bounded by ``limit``; retention horizon ``older_than_seconds``
        keeps recently-tombstoned resources recoverable.
        """
        stats = {
            "listed": 0, "purged": 0, "residue_repaired": 0,
            "unverifiable": 0, "failed": 0, "unavailable": 0,
        }
        listing = getattr(self.postgresql, "list_unpurged_tombstones", None)
        purge_md = getattr(self.postgresql, "purge_tombstone_metadata", None)
        insert = getattr(self.postgresql, "insert_outbox_event", None)
        if listing is None or purge_md is None:
            stats["unavailable"] = 1
            return stats
        rows = await listing(
            older_than_seconds=int(
                older_than_seconds
                if older_than_seconds is not None
                else self._TOMBSTONE_PURGE_AFTER_S
            ),
            limit=int(
                limit if limit is not None else self._TOMBSTONE_PURGE_BATCH
            ),
        )
        if rows is None:
            stats["unavailable"] = 1
            return stats
        for row in rows:
            stats["listed"] += 1
            module_id = str(row["module_id"])
            resource_id = str(row["resource_id"])
            try:
                remaining = self.qdrant.count_resource_points(
                    module_id, resource_id
                )
            except Exception:  # noqa: BLE001
                remaining = None
            if remaining is None:
                stats["unverifiable"] += 1
                continue
            if remaining > 0:
                repaired = False
                if insert is not None:
                    event = self._new_outbox_event(
                        operation=OutboxOperation.DELETE_RESOURCE,
                        module_id=module_id,
                        resource_id=resource_id,
                        source_version=int(row.get("source_revision") or 0),
                        content_hash=str(row.get("content_hash") or ""),
                    )
                    repaired = bool(await insert(event))
                stats["residue_repaired" if repaired else "failed"] += 1
                continue
            if await purge_md(module_id, resource_id):
                stats["purged"] += 1
            else:
                stats["failed"] += 1
        return stats
