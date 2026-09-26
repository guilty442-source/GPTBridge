"""Deletion Coordinator (migration 027 + C6).

Manages the two-stage deletion lifecycle:
    active → tombstone → retention window → purge

The coordinator tombstones a resource, waits for the retention window
to expire, then purges it from both engines (PostgreSQL index and
vectord vectors) in sync.

Usage:
    from shared_layer.database.deletion_coordinator import (
        tombstone,
        advance_stage,
        get_purge_eligible,
    )

    with connection_manager.connection() as conn:
        tombstone(conn, resource_id="res-123", retention_days=30)

    # Later (scheduled job):
    with connection_manager.connection() as conn:
        eligible = get_purge_eligible(conn)
        for item in eligible:
            # Delete vector points first, then advance
            advance_stage(conn, resource_id=item["resource_id"])

Runtime entry (injected PG / vectord interfaces):
    runtime = DeletionCoordinatorRuntime(
        connection_provider=connection_manager.connection,
        vector=vector_delete_adapter,
    )
    outcomes = await runtime.purge_eligible_async()

Ordering is fixed: vectors die before the authoritative tombstone is
raised, and PostgreSQL stages advance only after every injected engine
confirmed deletion.  Anything missing or failing leaves the stage
untouched and returns a typed FAILED outcome — never a silent PURGED.

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A44/E30 — four-functions-local.
    A52/E38 — RAG: canonical semantic vector index.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from psycopg import Connection

_TOMBSTONE = "SELECT gptbridge_index.tombstone_resource(%s, %s)"
_ADVANCE = "SELECT gptbridge_index.advance_deletion_stage(%s)"
_GET_ELIGIBLE = "SELECT gptbridge_index.get_purge_eligible(%s)"


class DeletionInterfaceError(RuntimeError):
    """Raised when the injected deletion interface is missing or misused."""


class PurgeStatus(str, Enum):
    PURGED = "PURGED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class PurgeOutcome:
    """Typed per-resource outcome of one purge / tombstone reconcile."""
    resource_id: str
    module_id: str
    status: PurgeStatus
    stage: Optional[str] = None
    vector_deleted: Optional[bool] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status is PurgeStatus.PURGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "module_id": self.module_id,
            "status": self.status.value,
            "ok": self.ok,
            "stage": self.stage,
            "vector_deleted": self.vector_deleted,
            "reason": self.reason,
        }


def tombstone(
    connection: Connection[Any],
    *,
    resource_id: str,
    retention_days: int = 30,
) -> None:
    """Mark a resource as tombstoned (stage: active → tombstone).

    The resource remains recoverable until the retention window expires.
    The runtime is responsible for removing the vectord points before the
    authoritative tombstone is raised.
    """
    connection.execute(_TOMBSTONE, (resource_id, retention_days))


def advance_stage(
    connection: Connection[Any],
    *,
    resource_id: str,
) -> str:
    """Advance the deletion stage (tombstone → retention → purged).

    Returns the new stage.  The caller must ensure vector cleanup
    (vectord) is complete before advancing to 'purged'.
    """
    row = connection.execute(_ADVANCE, (resource_id,)).fetchone()
    return str(row[0]) if row and row[0] else "unknown"


def get_purge_eligible(
    connection: Connection[Any],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List resources that are past their retention window and ready to purge."""
    rows = connection.execute(_GET_ELIGIBLE, (limit,)).fetchall()
    return [
        {
            "resource_id": str(r[0]),
            "module_id": str(r[1]),
            "tombstoned_at": r[2],
            "purge_after": r[3],
        }
        for r in rows
    ]


class DeletionCoordinatorRuntime:
    """Runtime entry for cross-engine purge and tombstone reconciliation.

    Injected interfaces (all duck-typed, sync or async where noted):
      connection_provider — callable returning a psycopg connection *or* a
          context manager yielding one (e.g. ``connection_manager.connection``).
      vector -- vectord adapter object with ``delete_resource(module_id, resource_id)``
          (sync or async; True/int is success, False/None is failure) and an
          optional ``count_resource_points(module_id, resource_id)`` used to
          verify the delete.
          for the module-private store.
      tombstone_raiser — object with async ``raise_tombstone(**kwargs)`` used
          by the reconciliation replay path.

    Purge cycle order per resource:
      1. vectord points deleted (idempotent)
      3. PostgreSQL stage advanced tombstone → retention → purged
    A failure at any step returns FAILED with the stage untouched.
    """

    def __init__(
        self,
        *,
        connection_provider: Optional[Callable[[], Any]] = None,
        vector: Any = None,
        tombstone_raiser: Any = None,
        retention_days: int = 30,
    ) -> None:
        self._connection_provider = connection_provider
        self._vector = vector
        self._tombstone_raiser = tombstone_raiser
        self._retention_days = int(retention_days)

    # -- connection plumbing ------------------------------------------------

    def _with_connection(self, fn: Callable[[Connection[Any]], Any]) -> Any:
        if self._connection_provider is None:
            raise DeletionInterfaceError("connection provider not configured")
        provided = self._connection_provider()
        if hasattr(provided, "__enter__"):
            with provided as connection:
                return fn(connection)
        return fn(provided)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _require_sync(value: Any, interface: str) -> Any:
        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            raise DeletionInterfaceError(
                f"{interface} is async; use purge_eligible_async()"
            )
        return value

    # -- tombstone-first entry ----------------------------------------------

    def request_tombstone(
        self, *, resource_id: str, retention_days: Optional[int] = None
    ) -> None:
        """Tombstone a resource before any purge can become eligible."""
        days = (
            self._retention_days
            if retention_days is None
            else int(retention_days)
        )
        self._with_connection(
            lambda connection: tombstone(
                connection, resource_id=resource_id, retention_days=days
            )
        )

    # -- purge cycle ---------------------------------------------------------

    def _eligible(self, limit: int) -> list[dict[str, Any]]:
        return self._with_connection(
            lambda connection: get_purge_eligible(connection, limit=limit)
        )

    def _advance_to_purged(self, resource_id: str) -> str:
        def _run(connection: Connection[Any]) -> str:
            stage = advance_stage(connection, resource_id=resource_id)
            if stage != "purged":
                stage = advance_stage(connection, resource_id=resource_id)
            return stage

        return self._with_connection(_run)

    async def purge_one_async(
        self, *, resource_id: str, module_id: str
    ) -> PurgeOutcome:
        """Purge one eligible resource, async interfaces supported."""
        if self._vector is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED,
                reason="vector-interface-missing",
            )
        try:
            deleted = await self._maybe_await(
                self._vector.delete_resource(module_id, resource_id)
            )
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason=f"vector-delete-failed: {exc}",
            )
        if deleted is False or deleted is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason="vector-delete-unsuccessful",
            )
        verifier = getattr(self._vector, "count_resource_points", None)
        if callable(verifier):
            try:
                remaining = await self._maybe_await(
                    verifier(module_id, resource_id)
                )
            except Exception as exc:
                return PurgeOutcome(
                    resource_id=resource_id, module_id=module_id,
                    status=PurgeStatus.FAILED, vector_deleted=True,
                    reason=f"vector-verify-failed: {exc}",
                )
            if remaining:
                return PurgeOutcome(
                    resource_id=resource_id, module_id=module_id,
                    status=PurgeStatus.FAILED, vector_deleted=True,
                    reason=f"vector-points-remain: {remaining}",
                )
        try:
            stage = self._advance_to_purged(resource_id)
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=True,
                reason=f"advance-stage-failed: {exc}",
            )
        if stage != "purged":
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, stage=stage, vector_deleted=True,
                reason=f"stage-not-purged: {stage}",
            )
        return PurgeOutcome(
            resource_id=resource_id, module_id=module_id,
            status=PurgeStatus.PURGED, stage="purged", vector_deleted=True,
        )

    def purge_one(self, *, resource_id: str, module_id: str) -> PurgeOutcome:
        """Sync purge one resource; async interfaces are refused, not awaited."""
        if self._vector is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED,
                reason="vector-interface-missing",
            )
        try:
            deleted = self._require_sync(
                self._vector.delete_resource(module_id, resource_id),
                "vector.delete_resource",
            )
        except DeletionInterfaceError as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, reason=str(exc),
            )
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason=f"vector-delete-failed: {exc}",
            )
        if deleted is False or deleted is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason="vector-delete-unsuccessful",
            )
        verifier = getattr(self._vector, "count_resource_points", None)
        if callable(verifier):
            try:
                remaining = self._require_sync(
                    verifier(module_id, resource_id),
                    "vector.count_resource_points",
                )
            except DeletionInterfaceError as exc:
                return PurgeOutcome(
                    resource_id=resource_id, module_id=module_id,
                    status=PurgeStatus.FAILED, vector_deleted=True,
                    reason=str(exc),
                )
            except Exception as exc:
                return PurgeOutcome(
                    resource_id=resource_id, module_id=module_id,
                    status=PurgeStatus.FAILED, vector_deleted=True,
                    reason=f"vector-verify-failed: {exc}",
                )
            if remaining:
                return PurgeOutcome(
                    resource_id=resource_id, module_id=module_id,
                    status=PurgeStatus.FAILED, vector_deleted=True,
                    reason=f"vector-points-remain: {remaining}",
                )
        try:
            stage = self._advance_to_purged(resource_id)
        except DeletionInterfaceError as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=True,
                reason=str(exc),
            )
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=True,
                reason=f"purge-failed: {exc}",
            )
        if stage != "purged":
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, stage=stage, vector_deleted=True,
                reason=f"stage-not-purged: {stage}",
            )
        return PurgeOutcome(
            resource_id=resource_id, module_id=module_id,
            status=PurgeStatus.PURGED, stage="purged", vector_deleted=True,
        )

    def purge_eligible(self, *, limit: int = 100) -> list[PurgeOutcome]:
        """Sync purge cycle over past-retention resources (sync interfaces)."""
        items = self._eligible(limit)
        return [
            self.purge_one(
                resource_id=str(item["resource_id"]),
                module_id=str(item["module_id"]),
            )
            for item in items
        ]

    async def purge_eligible_async(
        self, *, limit: int = 100
    ) -> list[PurgeOutcome]:
        """Async purge cycle over past-retention resources."""
        items = self._eligible(limit)
        outcomes: list[PurgeOutcome] = []
        for item in items:
            outcomes.append(
                await self.purge_one_async(
                    resource_id=str(item["resource_id"]),
                    module_id=str(item["module_id"]),
                )
            )
        return outcomes

    # -- reconciliation replay entry (main-system tombstone path) -----------

    async def reconcile_tombstone_async(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_revision: int,
        content_hash: str,
        reason: str,
    ) -> PurgeOutcome:
        """Vector delete first, authoritative tombstone second.

        Vector points are removed before the PostgreSQL tombstone is
        raised so a query barrier can never expose a hit whose vectors are
        already gone.  Re-running is idempotent (delete + tombstone upsert).
        """
        if self._vector is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED,
                reason="vector-interface-missing",
            )
        try:
            deleted = await self._maybe_await(
                self._vector.delete_resource(module_id, resource_id)
            )
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason=f"vector-delete-failed: {exc}",
            )
        if deleted is False or deleted is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=False,
                reason="vector-delete-unsuccessful",
            )
        if self._tombstone_raiser is None:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=True,
                reason="tombstone-raiser-missing",
            )
        try:
            await self._maybe_await(
                self._tombstone_raiser.raise_tombstone(
                    module_id=module_id,
                    resource_id=resource_id,
                    source_revision=int(source_revision),
                    content_hash=str(content_hash),
                    reason=str(reason),
                )
            )
        except Exception as exc:
            return PurgeOutcome(
                resource_id=resource_id, module_id=module_id,
                status=PurgeStatus.FAILED, vector_deleted=True,
                reason=f"tombstone-raise-failed: {exc}",
            )
        return PurgeOutcome(
            resource_id=resource_id, module_id=module_id,
            status=PurgeStatus.PURGED, vector_deleted=True,
            reason=None,
        )


__all__ = [
    "DeletionCoordinatorRuntime",
    "DeletionInterfaceError",
    "PurgeOutcome",
    "PurgeStatus",
    "advance_stage",
    "get_purge_eligible",
    "tombstone",
]
