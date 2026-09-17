"""PostgreSQL drill adapters for the recovery verification path
(A365/A369, A444/A501).

``PsycopgRestoreDrillAdapter`` binds the :mod:`restore_drill` contract to a
real PostgreSQL connection:

* ``engine_live`` is True only after a successful ``SELECT 1`` probe of the
  supplied connection (or connection factory).  With no connection the
  adapter keeps ``engine_live=False`` and every step raises
  :class:`~shared_layer.database.restore_drill.DrillEngineUnavailable` —
  success is never fabricated.
* Verification steps run real catalog/contract queries and return the
  existing typed evidence (``(passed, detail)``) consumed by
  ``run_restore_drill``.
* Physical steps (backup / simulated loss / restore) are deployment
  specific (``pg_basebackup``, PITR, scratch instance).  They are injected
  as callables at the assembly point; without an injected callable the step
  fails closed with an explicit error.

``PsycopgMigrationCellAdapter`` powers
``migration_destruction.live_cells``.  Cells apply DDL and therefore run
only through an injected bounded ``cell_runner``; every cell fails closed
when the engine is not live or no runner was injected.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .restore_drill import DrillEngineUnavailable
from .schema_contract_registry import declared_contract

__all__ = [
    "PsycopgMigrationCellAdapter",
    "PsycopgRestoreDrillAdapter",
    "probe_connection",
]


def probe_connection(
    connection: Any | None,
    connection_factory: Callable[[], Any] | None = None,
) -> tuple[Any | None, bool, str]:
    """Return ``(connection, engine_live, detail)`` for a real connection.

    The probe is a bounded ``SELECT 1``; anything other than a literal 1
    (including a raised error) means ``engine_live=False``.
    """
    if connection is None and connection_factory is not None:
        try:
            connection = connection_factory()
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return None, False, f"connect-failed:{exc}"[:200]
    if connection is None:
        return None, False, "no-connection"
    try:
        if hasattr(connection, "execute"):
            row = connection.execute("SELECT 1").fetchone()
        else:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return connection, False, f"probe-failed:{exc}"[:200]
    if not row or int(row[0]) != 1:
        return connection, False, "probe-returned-invalid"
    return connection, True, "SELECT 1"


class PsycopgRestoreDrillAdapter:
    """Restore-drill adapter over one real PostgreSQL connection.

    ``required_roles=None`` uses the declared schema contract roles;
    ``qdrant_point_checker`` is the injection point for a real Qdrant
    point-existence check (defaults to the PostgreSQL linkage query).
    """

    engine_live = False

    def __init__(
        self,
        connection: Any | None = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        backup_fn: Callable[[Path], None] | None = None,
        simulate_loss_fn: Callable[[], None] | None = None,
        restore_fn: Callable[[Path], None] | None = None,
        expected_migration_count: int | None = None,
        required_roles: Sequence[str] | None = None,
        max_reconcile_backlog: int = 0,
        max_transport_backlog: int = 0,
        qdrant_point_checker: Callable[[Any], tuple[int, int]] | None = None,
    ) -> None:
        self._backup_fn = backup_fn
        self._simulate_loss_fn = simulate_loss_fn
        self._restore_fn = restore_fn
        self._expected_migration_count = expected_migration_count
        self._required_roles = (
            tuple(required_roles) if required_roles is not None else None
        )
        self._max_reconcile_backlog = int(max_reconcile_backlog)
        self._max_transport_backlog = int(max_transport_backlog)
        self._qdrant_point_checker = qdrant_point_checker
        (
            self._connection,
            self.engine_live,
            self.engine_live_detail,
        ) = probe_connection(connection, connection_factory)

    # -- connection helpers -------------------------------------------------

    def _require_live(self) -> Any:
        if not self.engine_live or self._connection is None:
            raise DrillEngineUnavailable(
                f"engine-not-live:{self.engine_live_detail}"
            )
        return self._connection

    def _row(self, sql: str, params: Any = None) -> Any:
        connection = self._require_live()
        if params is None:
            result = connection.execute(sql)
        else:
            result = connection.execute(sql, params)
        return result.fetchone()

    def _rows(self, sql: str, params: Any = None) -> list[Any]:
        connection = self._require_live()
        if params is None:
            result = connection.execute(sql)
        else:
            result = connection.execute(sql, params)
        rows = result.fetchall()
        return list(rows or [])

    @staticmethod
    def _scalar(row: Any) -> int:
        return int(row[0]) if row and row[0] is not None else 0

    def evidence(self) -> dict[str, Any]:
        """Typed adapter evidence (never a pass/fail claim)."""
        return {
            "adapter": type(self).__name__,
            "engine_live": self.engine_live,
            "probe": self.engine_live_detail,
        }

    # -- physical steps (deployment-injected, fail-closed) ------------------

    def backup(self, destination: Path) -> None:
        self._require_live()
        if self._backup_fn is None:
            raise DrillEngineUnavailable("backup-fn-not-injected")
        self._backup_fn(Path(destination))

    def simulate_loss(self) -> None:
        self._require_live()
        if self._simulate_loss_fn is None:
            raise DrillEngineUnavailable("simulate-loss-fn-not-injected")
        self._simulate_loss_fn()

    def restore(self, source: Path) -> None:
        self._require_live()
        if self._restore_fn is None:
            raise DrillEngineUnavailable("restore-fn-not-injected")
        self._restore_fn(Path(source))

    # -- verification steps (real catalog queries) --------------------------

    def verify_schema(self) -> tuple[bool, str]:
        applied = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_index.schema_version"
        ))
        expected = self._expected_migration_count
        passed = applied > 0 and (
            expected is None or applied >= int(expected)
        )
        expected_detail = expected if expected is not None else 1
        return passed, (
            f"applied_migrations={applied} expected>={expected_detail}"
        )

    def verify_migration_head(self) -> tuple[bool, str]:
        row = self._row(
            "SELECT migration_name FROM gptbridge_index.schema_version"
            " ORDER BY version_id DESC LIMIT 1"
        )
        head = str(row[0]) if row and row[0] else ""
        return bool(head), f"head={head or 'missing'}"

    def verify_rls_roles(self) -> tuple[bool, str]:
        violations = self._scalar(self._row(
            "SELECT count(*) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname LIKE 'gptbridge_%' AND c.relkind = 'r'"
            " AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)"
        ))
        roles = self._required_roles
        if roles is None:
            roles = tuple(r.role_name for r in declared_contract().roles)
        missing: list[str] = []
        if roles:
            present = {str(r[0]) for r in self._rows(
                "SELECT rolname FROM pg_roles"
            )}
            missing = [role for role in roles if role not in present]
        passed = violations == 0 and not missing
        return passed, (
            f"rls_violations={violations} missing_roles={missing}"
        )

    def verify_audit_head(self) -> tuple[bool, str]:
        head_row = self._row(
            "SELECT event_id::text FROM gptbridge_audit.event"
            " ORDER BY occurred_at DESC LIMIT 1"
        )
        head = str(head_row[0]) if head_row and head_row[0] else ""
        broken = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_audit.verify_audit_chain(1000)"
            " WHERE chain_intact = false"
        ))
        return broken == 0, f"head={head or 'empty'} broken_chain={broken}"

    def verify_transport_state(self) -> tuple[bool, str]:
        backlog = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_transport.tool_request"
            " WHERE status IN ('queued', 'claimed')"
        ))
        passed = backlog <= self._max_transport_backlog
        return passed, (
            f"backlog={backlog} max={self._max_transport_backlog}"
        )

    def resource_count(self) -> tuple[bool, str]:
        count = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_index.resource"
        ))
        return True, f"resources={count}"

    def relation_count(self) -> tuple[bool, str]:
        count = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_index.resource_relation"
        ))
        return True, f"relations={count}"

    def verify_locator_integrity(self) -> tuple[bool, str]:
        missing = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_index.resource"
            " WHERE locator_id IS NULL OR locator_id = ''"
        ))
        return missing == 0, f"missing_locators={missing}"

    def reconcile(self) -> tuple[bool, str]:
        pending = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_index.reconcile_conflict_log"
            " WHERE resolution_action = 'pending'"
        ))
        passed = pending <= self._max_reconcile_backlog
        return passed, f"pending={pending} max={self._max_reconcile_backlog}"

    def verify_qdrant_references(self) -> tuple[bool, str]:
        if self._qdrant_point_checker is not None:
            checked, missing = self._qdrant_point_checker(
                self._require_live()
            )
            return (
                int(missing) == 0,
                f"checked={int(checked)} missing={int(missing)}",
            )
        missing = self._scalar(self._row(
            "SELECT count(*) FROM gptbridge_rag.index_state"
            " WHERE qdrant_point_id IS NULL OR qdrant_point_id = ''"
        ))
        return missing == 0, f"missing_qdrant_linkage={missing}"

    # -- extra evidence for PG recovery verification ------------------------

    def verify_generation(self) -> tuple[bool, str]:
        """Generation fence probe used by the recovery orchestrator."""
        generation = self._scalar(self._row(
            "SELECT gptbridge_index.current_backend_generation()"
        ))
        return generation >= 1, f"generation={generation}"


class PsycopgMigrationCellAdapter:
    """Bounded live migration-cell runner for ``live_cells``.

    ``cell_runner(cell) -> (passed, detail)`` owns the scratch-database
    DDL work.  Cells never run without a live engine and an injected
    runner; each cell runs at most once and is bounded by
    ``cell_timeout_seconds``.
    """

    engine_live = False

    def __init__(
        self,
        connection: Any | None = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        cell_runner: Callable[[str], Any] | None = None,
        cell_timeout_seconds: float = 300.0,
    ) -> None:
        self._cell_runner = cell_runner
        self._cell_timeout_seconds = float(cell_timeout_seconds)
        (
            self._connection,
            self.engine_live,
            self.engine_live_detail,
        ) = probe_connection(connection, connection_factory)

    def run_cell(self, cell: str) -> tuple[bool, str]:
        if not self.engine_live:
            return (
                False,
                f"fail-closed:engine-not-live:{self.engine_live_detail}"[:200],
            )
        if self._cell_runner is None:
            return False, "fail-closed:cell-runner-not-injected"
        started = time.monotonic()
        try:
            outcome = self._cell_runner(cell)
        except Exception as exc:  # noqa: BLE001 - verdict, not crash
            return False, f"error:{exc}"[:200]
        duration = time.monotonic() - started
        if duration > self._cell_timeout_seconds:
            return False, (
                f"timeout:{duration:.2f}s>{self._cell_timeout_seconds:.0f}s"
            )
        if isinstance(outcome, tuple) and len(outcome) == 2:
            return bool(outcome[0]), str(outcome[1])[:200]
        return bool(outcome), f"duration={duration:.3f}s"
